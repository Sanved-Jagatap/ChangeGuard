"""Final-report contract, evidence verification and hard-constraint enforcement.

The model writes the narrative and decision; code verifies it against what the tools actually returned.
"""
from __future__ import annotations

import re
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


def _contains_exact_scalar(value: Any, expected: Any) -> bool:
    """Find an exact scalar without substring matches or bool/int confusion."""
    if isinstance(value, dict):
        return any(_contains_exact_scalar(v, expected) for v in value.values())
    if isinstance(value, list):
        return any(_contains_exact_scalar(item, expected) for item in value)

    if isinstance(value, bool) or isinstance(expected, bool):
        return type(value) is type(expected) and value == expected

    if isinstance(value, (int, float)) or isinstance(expected, (int, float)):
        return (
            type(value) in (int, float)
            and type(expected) in (int, float)
            and value == expected
        )

    if isinstance(value, str) and isinstance(expected, str):
        return value.casefold() == expected.casefold()

    return value is None and expected is None


def _claim_value_matches(value: Any, expected: Any) -> bool:
    """Match exact numeric values while tolerating numeric strings from LLMs."""
    if isinstance(expected, bool) or isinstance(value, bool):
        return type(value) is type(expected) and value == expected

    if isinstance(expected, (int, float)):
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return False
            try:
                parsed = float(candidate)
            except ValueError:
                return False
            return parsed == expected

        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == expected
        )

    return type(value) is type(expected) and value == expected


def _subject_component_matches(subject: str, output: dict) -> bool:
    """If a subject names a component, require that component in the tool output."""
    ids = re.findall(r"\bCMP-[A-Za-z0-9_-]+\b", subject, flags=re.IGNORECASE)
    if not ids:
        return True

    observed = set()
    component_id = output.get("component_id")
    if isinstance(component_id, str):
        observed.add(component_id.upper())

    candidate = output.get("candidate")
    if isinstance(candidate, dict) and isinstance(candidate.get("component_id"), str):
        observed.add(candidate["component_id"].upper())

    return any(component_id.upper() in observed for component_id in ids)


def _claim_supported_by_step(claim: dict, step: dict) -> bool:
    """Verify a claim against the specific field and tool that can support it."""
    output = step.get("output")
    if not isinstance(output, dict):
        return False

    subject = str(claim.get("subject", ""))
    subject_lower = subject.casefold()
    claim_type = claim.get("type")
    value = claim.get("value")
    tool_name = step.get("name")

    if not _subject_component_matches(subject, output):
        return False

    if claim_type == "affected_item":
        if tool_name == "get_bom_impact":
            counts = output.get("counts")
        elif tool_name == "calculate_impact_metrics":
            counts = output.get("affected_count")
        else:
            return False

        if not isinstance(counts, dict):
            return False

        if "assembl" in subject_lower:
            expected = counts.get("assemblies")
        elif "product" in subject_lower:
            expected = counts.get("products")
        else:
            expected = counts.get("total")

        return expected is not None and _claim_value_matches(value, expected)

    if claim_type == "inventory":
        if tool_name == "get_inventory_status":
            if output.get("stock_data") == "unavailable":
                return isinstance(value, str) and value.casefold() == "unavailable"

            totals = output.get("totals")
            if not isinstance(totals, dict):
                return False

            if "on_hand" in subject_lower or "on hand" in subject_lower:
                expected = totals.get("on_hand")
            elif "reserved" in subject_lower:
                expected = totals.get("reserved")
            elif "available" in subject_lower:
                expected = totals.get("available")
            else:
                return False

            return expected is not None and _claim_value_matches(value, expected)

        if tool_name == "calculate_impact_metrics":
            inventory = output.get("inventory")
            if not isinstance(inventory, dict):
                return False

            if inventory.get("status") == "unavailable":
                return isinstance(value, str) and value.casefold() == "unavailable"

            if "available" not in subject_lower:
                return False

            expected = inventory.get("available_units")
            return expected is not None and _claim_value_matches(value, expected)

        return False

    if claim_type == "metric":
        if tool_name != "calculate_impact_metrics":
            return False

        # Metric claims must identify the field they describe.
        if "shortage" in subject_lower:
            expected = output.get("shortage_units")
        elif "exposure" in subject_lower:
            exposure = output.get("exposure")
            if not isinstance(exposure, dict):
                return False
            expected = exposure.get("units")
        elif "available" in subject_lower:
            inventory = output.get("inventory")
            if not isinstance(inventory, dict):
                return False
            expected = inventory.get("available_units")
        elif "affected_count" in subject_lower or "affected count" in subject_lower:
            counts = output.get("affected_count")
            if not isinstance(counts, dict):
                return False
            expected = counts.get("total")
        else:
            return False

        return expected is not None and _claim_value_matches(value, expected)

    if claim_type == "supplier":
        if tool_name != "get_supplier_options":
            return False

        # Explicitly support an empty alternate list only when the tool
        # reports that no alternates are declared.
        if "alternat" in subject_lower:
            alternates = output.get("alternates")
            issues = output.get("issues", [])
            no_alternates = any(
                isinstance(issue, dict) and issue.get("code") == "no_alternates"
                for issue in issues
            )
            return (
                isinstance(alternates, list)
                and not alternates
                and no_alternates
                and isinstance(value, str)
                and value.casefold() in {"none", "no alternates", "unavailable"}
            )

        records = output.get("suppliers")
        if not isinstance(records, list):
            return False

        # Resolve the subject to a particular supplier before checking a field.
        matching = [
            record for record in records
            if isinstance(record, dict)
            and any(
                isinstance(record.get(key), str)
                and record[key].casefold() in subject_lower
                for key in ("supplier_id", "name")
            )
        ]
        if not matching:
            return False

        record = matching[0]
        if "lead time" in subject_lower or "lead_time" in subject_lower:
            expected = record.get("lead_time_days")
            if expected is None:
                return isinstance(value, str) and value.casefold() == "unavailable"
        elif "risk" in subject_lower:
            expected = record.get("risk_level")
        elif "cost" in subject_lower:
            expected = record.get("unit_cost_cents")
        elif "usable" in subject_lower:
            expected = record.get("usable")
        elif "status" in subject_lower:
            expected = record.get("status")
        else:
            return False

        return expected is not None and type(expected) is type(value) and expected == value

    if claim_type == "compliance":
        if tool_name != "check_compliance":
            return False

        requirements = output.get("requirements")
        summary = output.get("summary")
        if not isinstance(requirements, list) or not requirements:
            return False

        # A general "confirmed" claim is valid only when every returned
        # requirement is confirmed and the summary agrees.
        if subject_lower.endswith("compliance") or " overall compliance" in subject_lower:
            if not isinstance(value, str) or value.casefold() != "confirmed":
                return False
            if not all(
                isinstance(item, dict) and item.get("status") == "confirmed"
                for item in requirements
            ):
                return False
            if not isinstance(summary, dict):
                return False
            return (
                summary.get("failed") == 0
                and summary.get("not_checked") == 0
                and summary.get("unresolved") == 0
                and summary.get("confirmed") == len(requirements)
            )

        # For a requirement-specific claim, bind the value to that requirement.
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            labels = [
                str(requirement.get(key, "")).strip().casefold()
                for key in ("requirement_id", "code", "name", "requirement")
                if requirement.get(key) is not None
                and str(requirement.get(key)).strip()
            ]
            if any(label in subject_lower for label in labels):
                expected = requirement.get("status")
                return (
                    expected is not None
                    and type(expected) is type(value)
                    and expected == value
                )
        return False

    if claim_type == "other":
        # Keep "other" narrow: candidate eligibility is a deterministic
        # assessment, not a compliance or supplier fact.
        if tool_name != "calculate_impact_metrics":
            return False
        if "eligibility" not in subject_lower and "eligible_for_review" not in subject_lower:
            return False

        candidate = output.get("candidate")
        if not isinstance(candidate, dict):
            return False

        candidate_id = candidate.get("component_id")
        if not isinstance(candidate_id, str) or candidate_id.casefold() not in subject_lower:
            return False

        expected = candidate.get("eligibility")
        return expected is not None and type(expected) is type(value) and expected == value

    return False

def verify_claims(report: dict, steps: list[dict]) -> dict:
    """Verify claims against successful, cited tool outputs."""
    ok_steps = _tool_ok_steps(steps)
    results = []

    for claim in report.get("claims", []):
        evidence = claim.get("evidence", [])
        supported = bool(evidence)

        for ref in evidence:
            step = ok_steps.get(ref)
            if step is None or not _claim_supported_by_step(claim, step):
                supported = False
                break

        results.append({
            "subject": claim.get("subject", ""),
            "value": claim.get("value"),
            "supported": supported,
        })

    total = len(results)
    supported_count = sum(1 for result in results if result["supported"])

    return {
        "total": total,
        "supported": supported_count,
        "support_rate": supported_count / total if total else None,
        "unsupported": [result for result in results if not result["supported"]],
    }


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
