"""Application service: orchestrates intake, investigation runs, approvals and reads.
Everything the API/CLI/benchmark does goes through here so rules live in one place."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from .agent.loop import AgentRunner, RunResult
from .approvals import ApprovalService, new_id
from .audit import audit
from .auth import Principal
from .baseline import run_baseline
from .config import Settings
from .db import Database, utcnow
from .errors import BudgetExceeded, PermissionDenied, ValidationError
from .llm.base import LLMClient
from .llm.factory import make_llm
from .schemas import parse_change_request
from .seed import SeedData, setup_database
from .tools.definitions import build_registry
from .tools.registry import FaultPlan, ToolContext
from .tracing import TraceRecorder


class ChangeGuardService:
    def __init__(self, db: Database, settings: Settings, llm_factory: Callable[..., LLMClient] | None = None):
        self.db, self.settings = db, settings
        self.llm_factory = llm_factory or make_llm
        self.approvals = ApprovalService(db, settings)

    # ------------------------------------------------------------------ setup
    def init_database(self, seed: SeedData | None = None) -> SeedData:
        return setup_database(self.db, seed)

    def reset_demo(self, principal: Principal) -> None:
        principal.require("reset")
        if not self.settings.allow_reset:
            raise PermissionDenied("reset is disabled (set CG_ALLOW_RESET=1 on a sandbox deployment)")
        self.db.drop_all()
        self.init_database()
        with self.db.connect("recorder") as rec:
            audit(rec, principal.user, "demo_reset")

    # ------------------------------------------------------------------ intake
    def submit_change(self, principal: Principal, data: dict, idempotency_key: str) -> dict:
        principal.require("submit")
        if not idempotency_key or len(idempotency_key) > 100:
            raise ValidationError("idempotency_key is required (max 100 characters)")
        req = parse_change_request(data)
        with self.db.connect("reader") as r:
            existing = r.query_one("SELECT * FROM engineering_changes WHERE idempotency_key = ?", (idempotency_key,))
            if existing:
                return existing | {"idempotent_replay": True}
            if r.query_one("SELECT 1 AS x FROM components WHERE component_id = ?", (req.component_id,)) is None:
                raise ValidationError(f"unknown component_id {req.component_id}")
            site = req.constraints.get("site_id")
            if site and r.query_one("SELECT 1 AS x FROM sites WHERE site_id = ?", (site,)) is None:
                raise ValidationError(f"unknown site_id {site}")
        cid = new_id("CHG")
        with self.db.connect("recorder") as rec, rec.tx():
            rec.execute(
                "INSERT INTO engineering_changes(change_id, component_id, change_type, description, urgency, target_market, "
                "constraints_json, status, idempotency_key, requested_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, req.component_id, req.change_type, req.description, req.urgency, req.target_market,
                 json.dumps(req.constraints, sort_keys=True), "submitted", idempotency_key, principal.user, utcnow()))
            audit(rec, principal.user, "change_submitted", change_id=cid, detail={"component_id": req.component_id})
        return self.get_change(cid)

    def get_change(self, change_id: str) -> dict:
        with self.db.connect("reader") as r:
            row = r.query_one("SELECT * FROM engineering_changes WHERE change_id = ?", (change_id,))
        if row is None:
            raise ValidationError(f"unknown change_id {change_id}")
        row["constraints"] = json.loads(row["constraints_json"])
        return row

    def list_changes(self, limit: int = 50) -> list[dict]:
        with self.db.connect("reader") as r:
            return r.query("SELECT change_id, component_id, change_type, urgency, target_market, status, created_at "
                           "FROM engineering_changes ORDER BY created_at DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ investigation
    def _check_budget(self, provider: str) -> None:
        if provider in ("mock", "mock_gullible", "none"):
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        with self.db.connect("reader") as r:
            n = r.query_one("SELECT COUNT(*) AS n FROM agent_runs WHERE runner='agent' AND provider NOT IN ('mock','mock_gullible','none') "
                            "AND started_at >= ?", (today,))["n"]
        if int(n) >= self.settings.max_agent_runs_per_day:
            raise BudgetExceeded(f"daily limit of {self.settings.max_agent_runs_per_day} hosted-model runs reached")

    def investigate(self, principal: Principal, change_id: str, runner: str = "agent", provider: str | None = None,
                    model: str | None = None, faults: FaultPlan | None = None) -> RunResult:
        principal.require("investigate")
        if runner not in ("agent", "baseline"):
            raise ValidationError("runner must be 'agent' or 'baseline'")
        change = self.get_change(change_id)
        llm = self.llm_factory(self.settings, provider, model) if runner == "agent" else None
        if llm is not None:
            self._check_budget(llm.provider)
        run_id = new_id("RUN")
        config = {"max_steps": self.settings.max_steps, "max_retries_per_tool": self.settings.max_retries_per_tool,
                  "wall_clock_seconds": self.settings.wall_clock_seconds, "max_total_tokens": self.settings.max_total_tokens}
        with self.db.connect("recorder") as rec, rec.tx():
            rec.execute(
                "INSERT INTO agent_runs(run_id, change_id, runner, provider, model, status, started_at, step_count, config_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, change_id, runner, llm.provider if llm else "none", llm.model if llm else "fixed-pipeline",
                 "running", utcnow(), 0, json.dumps(config)))
            rec.execute("UPDATE engineering_changes SET status='investigating' WHERE change_id = ?", (change_id,))
            audit(rec, principal.user, "run_started", change_id=change_id, run_id=run_id, detail={"runner": runner})
        registry = build_registry(self.settings, faults)
        with self.db.connect("reader") as reader, self.db.connect("recorder") as rec:
            trace = TraceRecorder(rec, run_id)
            ctx = ToolContext(reader=reader, settings=self.settings, change=change, run_id=run_id,
                              approvals=self.approvals, actor=f"{runner}:{run_id}")
            if runner == "agent":
                result = AgentRunner(registry, llm, self.settings, trace).run(change, ctx)  # type: ignore[arg-type]
            else:
                result = run_baseline(registry, trace, ctx, change)
            final_change_status = "investigated" if result.status == "completed" else "escalated"
            with rec.tx():
                rec.execute(
                    "UPDATE agent_runs SET status=?, stop_reason=?, ended_at=?, step_count=?, input_tokens=?, output_tokens=?, "
                    "cost_usd=?, report_json=? WHERE run_id=?",
                    (result.status, result.stop_reason, utcnow(), len(trace.steps), result.input_tokens, result.output_tokens,
                     result.cost_usd, json.dumps({"report": result.report, "facts": result.facts, "claim_check": result.claim_check},
                                                 default=str), run_id))
                rec.execute("UPDATE engineering_changes SET status=? WHERE change_id=?", (final_change_status, change_id))
                audit(rec, principal.user, "run_finished", change_id=change_id, run_id=run_id,
                      detail={"status": result.status, "stop_reason": result.stop_reason, "outcome": result.outcome})
        return result

    # ------------------------------------------------------------------ reads
    def get_run(self, run_id: str) -> dict:
        with self.db.connect("reader") as r:
            row = r.query_one("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise ValidationError(f"unknown run_id {run_id}")
        blob = json.loads(row.pop("report_json") or "{}")
        return row | blob

    def list_runs(self, change_id: str) -> list[dict]:
        with self.db.connect("reader") as r:
            return r.query("SELECT run_id, runner, provider, model, status, stop_reason, started_at, step_count, input_tokens, "
                           "output_tokens, cost_usd FROM agent_runs WHERE change_id = ? ORDER BY started_at", (change_id,))

    def run_steps(self, run_id: str) -> list[dict]:
        with self.db.connect("reader") as r:
            rows = r.query("SELECT * FROM agent_steps WHERE run_id = ? ORDER BY idx", (run_id,))
        for s in rows:
            for k in ("input_json", "output_json"):
                s[k.replace("_json", "")] = json.loads(s.pop(k)) if s[k] and not s[k].endswith("[truncated]") else s.pop(k)
        return rows

    def audit_log(self, change_id: str | None = None, limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM audit_events", []
        if change_id:
            sql += " WHERE change_id = ?"
            params.append(change_id)
        with self.db.connect("reader") as r:
            rows = r.query(sql + " ORDER BY seq DESC LIMIT ?", params + [limit])
        for x in rows:
            x["detail"] = json.loads(x.pop("detail_json"))
        return rows

    # approvals passthrough (kept here so API/UI/bench share one entry point)
    def decide(self, principal: Principal, action_id: str, decision: str, presented_hash: str, reason: str | None = None) -> dict:
        return self.approvals.decide(principal, action_id, decision, presented_hash, reason)

    def execute(self, principal: Principal, action_id: str) -> dict:
        return self.approvals.execute(principal, action_id)
