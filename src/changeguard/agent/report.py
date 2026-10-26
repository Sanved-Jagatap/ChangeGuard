"""Final-report contract, evidence verification and hard-constraint enforcement.

The model writes the narrative and decision; code verifies it against what the tools actually returned.
"""
from __future__ import annotations

import json
from typing import Any

from ..schemas import ID_PATTERN, validate

UNRESOLVED_CODES = ["missing_lead_time", "supplier_inactive", "compliance_unresolved", "compliance_failed",
                    "compliance_not_checked", "data_conflict", "stale_evidence", "no_alternates", "single_source",
                    "exposure_unknown", "stock_unavailable", "tool_failure", "not_found", "lead_time_exceeds_constraint",
                    "no_supplier_records", "not_declared_alternate"]
UNRESOLVED_RE = r"^(" + "|".join(UNRESOLVED_CODES) + r"):[A-Za-z0-9_\-]{1,30}$"
OUTCOMES = ["recommendation", "insufficient_evidence", "escalated", "refused"]
CLAIM_TYPES = ["affected_item", "inventory", "supplier", "compliance", "metric", "other"]

REPORT_SCHEMA: dict = {
    "type": "object", "additionalProperties": False,
    "required": ["outcome", "summary", "affected_ids", "key_figures", "recommended_component_id"],
    "properties": {
        "outcome": {"type": "string", "enum": OUTCOMES},
        "summary": {"type": "string", "minLength": 5, "maxLength": 1200},
        "affected_ids": {"type": "array", "maxItems": 100, "items": {"type": "string", "pattern": ID_PATTERN}},
        "key_figures": {
            "type": "object", "additionalProperties": False,
            "properties": {k: {"type": ["integer", "null"], "minimum": 0}
                           for k in ("affected_count", "exposure_units", "available_units", "shortage_units")},
        },
        "recommended_component_id": {"type": ["string", "null"], "pattern": ID_PATTERN},
        "option_assessments": {
            "type": "array", "maxItems": 12,
            "items": {"type": "object", "additionalProperties": False, "required": ["component_id", "eligibility"],
                      "properties": {"component_id": {"type": "string", "pattern": ID_PATTERN},
                                     "eligibility": {"type": "string", "enum": ["eligible_for_review", "insufficient_evidence", "blocked"]},
                                     "reasons": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 80}}}},
        },
        "claims": {
            "type": "array", "maxItems": 30,
            "items": {"type": "object", "additionalProperties": False, "required": ["type", "subject", "value", "evidence"],
                      "properties": {"type": {"type": "string", "enum": CLAIM_TYPES},
                                     "subject": {"type": "string", "maxLength": 40},
                                     "value": {"type": ["string", "integer", "number", "boolean"]},
                                     "evidence": {"type": "array", "minItems": 1, "maxItems": 5,
                                                  "items": {"type": "string", "pattern": r"^S[0-9]{1,3}$"}}}},
        },
        "assumptions": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 300}},
        "unresolved": {"type": "array", "maxItems": 30, "items": {"type": "string", "pattern": UNRESOLVED_RE}},
        "next_steps": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 300}},
        "proposed_action_id": {"type": ["string", "null"], "maxLength": 30},
        "refused_requests": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200}},
    },
}


def validate_report(report: Any) -> list[str]:
    return validate(REPORT_SCHEMA, report)


def _tool_ok_steps(steps: list[dict]) -> dict[str, dict]:
    return {f"S{s['idx']}": s for s in steps if s["kind"] == "tool" and s["status"] == "ok"}


def verify_claims(report: dict, steps: list[dict]) -> dict:
    """A claim is supported if every cited step exists, succeeded, and its output contains the claimed value."""
    ok_steps = _tool_ok_steps(steps)
    results = []
    for c in report.get("claims", []):
        supported = True
        for ref in c["evidence"]:
            step = ok_steps.get(ref)
            if step is None:
                supported = False
                break
            blob = json.dumps(step["output"], default=str).lower()
            if str(c["value"]).lower() not in blob or c["subject"].lower() not in blob:
                supported = False
                break
        results.append({"subject": c["subject"], "value": c["value"], "supported": supported})
    total = len(results)
    return {"total": total, "supported": sum(1 for r in results if r["supported"]),
            "support_rate": (sum(1 for r in results if r["supported"]) / total) if total else None,
            "unsupported": [r for r in results if not r["supported"]]}


def observed_candidates(steps: list[dict]) -> dict[str, dict]:
    """Latest deterministic candidate evaluation per component seen in this run."""
    out: dict[str, dict] = {}
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "calculate_impact_metrics" and s["status"] == "ok":
            cand = (s["output"] or {}).get("candidate")
            if cand:
                out[cand["component_id"]] = cand
    return out


def enforce_constraints(report: dict, steps: list[dict]) -> list[str]:
    """Hard rules the model cannot talk its way around. Returns violations (empty = fine)."""
    v: list[str] = []
    rec = report.get("recommended_component_id")
    obs = observed_candidates(steps)
    if report["outcome"] == "recommendation":
        if not rec:
            v.append("outcome 'recommendation' requires recommended_component_id")
        elif rec not in obs:
            v.append(f"{rec} was never evaluated with calculate_impact_metrics in this run")
        elif obs[rec]["eligibility"] != "eligible_for_review":
            v.append(f"{rec} is {obs[rec]['eligibility']} per deterministic rules ({', '.join(obs[rec]['reasons'])})")
    elif rec:
        v.append("recommended_component_id must be null unless outcome is 'recommendation'")
    for a in report.get("option_assessments", []):
        o = obs.get(a["component_id"])
        if o and o["eligibility"] != a["eligibility"]:
            v.append(f"assessment of {a['component_id']} says {a['eligibility']} but deterministic result is {o['eligibility']}")
    return v


def facts_from_steps(steps: list[dict]) -> dict:
    """Deterministic facts assembled by code from tool outputs (shown next to the model's narrative)."""
    facts: dict[str, Any] = {"affected_ids": None, "metrics": None, "candidates": {}}
    for s in steps:
        if s["kind"] != "tool" or s["status"] != "ok" or not s["output"]:
            continue
        if s["name"] == "get_bom_impact":
            facts["affected_ids"] = s["output"].get("affected_ids")
        if s["name"] == "calculate_impact_metrics":
            o = s["output"]
            if not o.get("candidate"):
                facts["metrics"] = {k: o.get(k) for k in ("affected_count", "exposure", "inventory", "shortage_units", "coverage_ratio")}
            else:
                facts["candidates"][o["candidate"]["component_id"]] = o["candidate"]
    return facts
