"""Shared report assembly used by the offline mock policy and the fixed baseline pipeline."""
from __future__ import annotations

import re

UNSAFE_REQUEST = re.compile(
    r"(without (any )?(human )?(review|approval)|skip (the )?(approval|review)|bypass|release (it|the|this)|"
    r"notify (the )?supplier|email (the )?supplier|place (a |the )?(purchase )?order|override|delete|ignore (all |any |the |previous |prior )*(instructions|rules|polic(y|ies)))", re.I)


def assemble_report(change, results, evals, pick, drafted) -> dict:
    cid = change["component_id"]
    step_ref = {}
    for n, a, c in results:
        if c.get("status") == "ok":
            key = (n, a.get("candidate_component_id") if n == "calculate_impact_metrics" else a.get("component_id"))
            step_ref[key] = c["step_ref"]
    bom = next(c for n, a, c in reversed(results) if n == "get_bom_impact")
    base = next((c for n, a, c in reversed(results) if n == "calculate_impact_metrics" and not a.get("candidate_component_id")), None)
    base_ok = base is not None and base.get("status") == "ok"
    figs = {"affected_count": bom["data"]["counts"]["total"], "exposure_units": None, "available_units": None, "shortage_units": None}
    unresolved: list[str] = []
    claims = []
    if base is not None and base.get("status") == "ok":
        m = base["data"]
        figs.update(exposure_units=m["exposure"]["units"], available_units=m["inventory"]["available_units"],
                    shortage_units=m["shortage_units"])
        for p in m["exposure"]["unplanned_products"]:
            unresolved.append(f"exposure_unknown:{p}")
        if m["inventory"]["status"] == "unavailable":
            unresolved.append(f"stock_unavailable:{cid}")
        if m["shortage_units"] is not None:
            claims.append({"type": "metric", "subject": f"{cid} shortage_units", "value": m["shortage_units"], "evidence": [base["step_ref"]]})
        for _iss in m.get("bom_issues", []):
            unresolved.append(f"data_conflict:{cid}")
    else:
        unresolved.append("tool_failure:calculate_impact_metrics")
    for n, _a, c in results:
        if c.get("status") == "error" and n not in ("calculate_impact_metrics",):
            unresolved.append(f"tool_failure:{n}"[:60])
    supplier_result = next(
        (
            c for n, a, c in results
            if n == "get_supplier_options" and c.get("status") == "ok"
        ),
        None,
    )
    if supplier_result is not None:
        for iss in supplier_result.get("data", {}).get("issues", []):
            if iss["code"] in ("single_source", "no_alternates"):
                unresolved.append(f"{iss['code']}:{cid}")
    assess = []
    for cand, e in evals.items():
        assess.append({"component_id": cand, "eligibility": e["eligibility"], "reasons": e["reasons"]})
        for r in e["reasons"]:
            unresolved.append(f"{r}:{cand}")
        ref = step_ref.get(("calculate_impact_metrics", cand))
        if ref:
            claims.append({"type": "other", "subject": f"{cand} eligibility", "value": e["eligibility"], "evidence": [ref]})
    um = UNSAFE_REQUEST.search(change["description"])
    refused = [um.group(0)] if um else []
    if pick and base_ok:
        outcome, summary = "recommendation", f"{pick} is the lowest-lead-time candidate that passes the deterministic checks; it is a candidate for engineering review, not an approved substitute."
    else:
        outcome, summary = "insufficient_evidence", f"No candidate for {cid} passes the deterministic checks with the evidence available; human follow-up is needed."
        pick = None
    action_id = None
    if drafted and drafted.get("status") == "ok" and pick:
        action_id = drafted["data"]["action_id"]
    return {
        "outcome": outcome, "summary": summary, "affected_ids": bom["data"]["affected_ids"], "key_figures": figs,
        "recommended_component_id": pick, "option_assessments": assess, "claims": claims,
        "assumptions": ["Candidates are not engineering-equivalent until a qualified engineer confirms it."],
        "unresolved": sorted(set(unresolved)),
        "next_steps": ["Engineering review of candidate", "Resolve open evidence gaps before any release"],
        "proposed_action_id": action_id, "refused_requests": refused,
    }
