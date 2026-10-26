"""Fixed-pipeline baseline: same tools, same limits, same report, but the SEQUENCE is hard-coded.
It never reads free text (so injected text cannot influence it) and refuses unsafe requests with a regex."""
from __future__ import annotations

import time

from .agent.assemble import assemble_report
from .agent.loop import RunResult, escalation_report
from .agent.report import facts_from_steps, verify_claims
from .tools.executor import run_tool
from .tools.registry import ToolContext, ToolRegistry
from .tracing import TraceRecorder


def run_baseline(registry: ToolRegistry, trace: TraceRecorder, ctx: ToolContext, change: dict) -> RunResult:
    t0 = time.perf_counter()
    cid, market = change["component_id"], change.get("target_market")
    results: list[tuple[str, dict, dict]] = []
    counters = {"calls": 0, "retries": 0}

    def call(name: str, args: dict) -> dict:
        counters["calls"] += 1
        ok, payload, idx, attempts = run_tool(registry, trace, name, args, ctx)
        counters["retries"] += attempts - 1
        content = {"step_ref": f"S{idx}", "status": "ok", "data": payload} if ok else {"step_ref": f"S{idx}", "status": "error", **payload}
        results.append((name, args, content))
        return content

    def done(status: str, reason: str, report: dict) -> RunResult:
        return RunResult(run_id=trace.run_id, runner="baseline", status=status, stop_reason=reason, report=report,
                         steps=trace.steps, provider="none", model="fixed-pipeline", input_tokens=0, output_tokens=0,
                         cost_usd=0.0, latency_ms=int((time.perf_counter() - t0) * 1000), tool_calls=counters["calls"],
                         retries=counters["retries"], claim_check=verify_claims(report, trace.steps),
                         facts=facts_from_steps(trace.steps))

    bom = call("get_bom_impact", {"component_id": cid, "depth_limit": 8})
    if bom["status"] != "ok":
        return done("escalated", "bom_failed", escalation_report(f"where-used analysis failed: {bom['error']['kind']}", trace.steps, ["get_bom_impact"]))
    call("get_inventory_status", {"component_id": cid})
    sup = call("get_supplier_options", {"component_id": cid})
    call("calculate_impact_metrics", {"component_id": cid})
    candidates = [a["component_id"] for a in sup["data"]["alternates"]] if sup["status"] == "ok" else []
    evals: dict[str, dict] = {}
    for cand in candidates:
        call("check_compliance", {"component_id": cand, "target_market": market})
        m = call("calculate_impact_metrics", {"component_id": cid, "candidate_component_id": cand})
        if m["status"] == "ok" and m["data"].get("candidate"):
            evals[cand] = m["data"]["candidate"]
    ok = {k: v for k, v in evals.items() if v["eligibility"] == "eligible_for_review"}
    pick = sorted(ok, key=lambda k: (ok[k]["best_lead_time_days"] if ok[k]["best_lead_time_days"] is not None else 10**6, k))[0] if ok else None
    base_ok = any(n == "calculate_impact_metrics" and not a.get("candidate_component_id") and c["status"] == "ok" for n, a, c in results)
    if pick and not base_ok:
        pick = None
    drafted = None
    if pick:
        drafted = call("draft_change_request", {
            "action_type": "CREATE_CHANGE_ORDER", "component_id": cid, "replacement_component_id": pick,
            "disposition": "substitute_candidate_review", "affected_assemblies": bom["data"]["affected_ids"],
            "notes": "Candidate for engineering review only; equivalence not established."})
        if drafted["status"] != "ok":
            pick = None
    report = assemble_report(change, results, evals, pick, drafted)
    return done("completed", "pipeline_finished", report)
