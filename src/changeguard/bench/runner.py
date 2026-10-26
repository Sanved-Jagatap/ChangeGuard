"""Run benchmark cases against the baseline or the agent, scoring each against the oracle's labels."""
from __future__ import annotations

import copy
import json
import platform
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..auth import Principal
from ..config import Settings
from ..db import Database
from ..errors import ApprovalError, PermissionDenied, ValidationError
from ..seed import build_seed
from ..service import ChangeGuardService
from ..tools.registry import FaultPlan
from .generate import load_dataset
from .mutations import apply_mutations
from .scoring import aggregate, score_case

ANALYST = Principal("bench-analyst", "analyst")
REVIEWER = Principal("bench-reviewer", "reviewer")
VIEWER = Principal("bench-viewer", "viewer")
ADMIN = Principal("bench-admin", "admin")


def bench_settings() -> Settings:
    s = Settings.from_env()
    s.test_hooks, s.retry_backoff_seconds, s.allow_reset = True, 0.0, False
    s.max_agent_runs_per_day = 10**9
    return s


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3,
                              cwd=Path(__file__).parent).stdout.strip() or None
    except Exception:
        return None


def _run_script(svc: ChangeGuardService, change_id: str, script: list[dict]) -> dict:
    acts = svc.approvals.list_actions(change_id)
    out = {"has_proposal": bool(acts), "change_orders": 0, "final_status": None, "unexpected_errors": [], "denied": []}
    if not acts:
        return out
    act = acts[-1]
    who = {"reviewer": REVIEWER, "viewer": VIEWER}
    for step in script:
        op = step["op"]
        try:
            if op == "decide":
                h = act["payload_hash"] if step["hash"] == "correct" else "0" * 64
                svc.decide(who[step["who"]], act["action_id"], step["decision"], h, "bench")
            elif op == "execute":
                svc.execute(who[step["who"]], act["action_id"])
            elif op == "tamper":
                payload = dict(act["payload"], replacement_component_id="CMP-106")
                with svc.db.connect("admin") as c:
                    c.execute("UPDATE proposed_actions SET payload_json = ? WHERE action_id = ?",
                              (json.dumps(payload, sort_keys=True), act["action_id"]))
            elif op == "suppliers_inactive":
                with svc.db.connect("admin") as c:
                    c.execute("UPDATE suppliers SET status = 'inactive' WHERE supplier_id IN ('S-001','S-002')")
            elif op == "force_verify_fail":
                svc.approvals.force_verification_failure = True
        except (PermissionDenied, ApprovalError) as e:
            out["denied"].append(f"{op}:{getattr(e, 'kind', type(e).__name__)}")
        except Exception as e:  # anything else is a bug, not a policy refusal
            out["unexpected_errors"].append(f"{op}:{type(e).__name__}:{e}")
    with svc.db.connect("reader") as r:
        out["change_orders"] = int(r.query_one("SELECT COUNT(*) AS n FROM change_orders")["n"])
    out["final_status"] = svc.approvals.get_action(act["action_id"])["status"]
    return out


def run_case(case: dict, runner: str, provider: str = "mock", model: str | None = None) -> dict:
    tmp = tempfile.mkdtemp(prefix="cg_bench_")
    try:
        seed = apply_mutations(build_seed(), copy.deepcopy(case["mutations"]))
        svc = ChangeGuardService(Database(f"sqlite:///{tmp}/b.db"), bench_settings())
        svc.init_database(seed)
        extras: dict = {}
        try:
            change = svc.submit_change(ANALYST, case["request"], case["case_id"])
        except ValidationError:
            extras["intake_rejected"] = True
            return score_case(case, None, extras)
        t0 = time.perf_counter()
        res = svc.investigate(ANALYST, change["change_id"], runner, provider=provider, model=model,
                              faults=FaultPlan(copy.deepcopy(case["faults"])))
        if case.get("script"):
            extras["approval"] = _run_script(svc, change["change_id"], case["script"])
        with svc.db.connect("reader") as r:
            n_co = int(r.query_one("SELECT COUNT(*) AS n FROM change_orders")["n"])
        if not case.get("script"):
            extras["unauthorized_writes"] = n_co  # analysis-only cases must never change state
        row = score_case(case, res, extras)
        row["wall_ms"] = int((time.perf_counter() - t0) * 1000)
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def select(cases: list[dict], subset: str, families: list[str] | None = None, limit: int | None = None) -> list[dict]:
    if subset == "smoke":
        cases = [c for c in cases if c["smoke"]]
    elif subset in ("dev", "heldout"):
        cases = [c for c in cases if c["split"] == subset]
    if families:
        cases = [c for c in cases if c["family"] in families]
    return cases[:limit] if limit else cases


def run_bench(runner: str, provider: str = "mock", model: str | None = None, subset: str = "all", families: list[str] | None = None,
              limit: int | None = None, out: Path | None = None, sleep: float = 0.0, resume: bool = False,
              dataset_path: Path | None = None, progress: Callable[[str], None] | None = None) -> dict:
    ds = load_dataset(dataset_path) if dataset_path else load_dataset()
    cases = select(ds["cases"], subset, families, limit)
    meta = {"dataset_version": ds["dataset_version"], "dataset_hash": ds["dataset_hash"], "runner": runner,
            "provider": provider if runner == "agent" else "none", "model": (model or "") if runner == "agent" else "fixed-pipeline",
            "subset": subset, "started": datetime.now(UTC).isoformat(timespec="seconds"), "git": _git_sha(),
            "python": platform.python_version(), "n_cases": len(cases),
            "plumbing_only": runner == "agent" and provider.startswith("mock")}
    done: dict[str, dict] = {}
    if resume and out and out.exists():
        prev = json.loads(out.read_text(encoding="utf-8"))
        done = {r["case_id"]: r for r in prev["results"] if not r.get("error")}
    rows: list[dict] = list(done.values())
    for i, case in enumerate(cases, 1):
        if case["case_id"] in done:
            continue
        try:
            row = run_case(case, runner, provider, model)
        except Exception as e:  # one broken case must not lose the whole run
            row = {"case_id": case["case_id"], "family": case["family"], "split": case["split"], "smoke": case["smoke"],
                   "success": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        rows.append(row)
        if progress:
            progress(f"[{i}/{len(cases)}] {case['case_id']} {'PASS' if row['success'] else 'FAIL'}")
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"meta": meta, "results": rows}, indent=1, default=str), encoding="utf-8")
        if sleep:
            time.sleep(sleep)
    result = {"meta": meta, "results": rows, "aggregate": aggregate(rows)}
    if out:
        out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result
