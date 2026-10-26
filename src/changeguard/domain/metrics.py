"""Governed metric definitions and deterministic candidate eligibility (see docs/METRICS.md).

METRICS_VERSION 1.0
  affected_count      distinct ancestors (assemblies + products) found by where-used, within depth limit
  qty_per_unit        sum over BOM paths to a top-level item of the product of qty_per along the path
  exposure_units      sum over top-level items of planned_units (optionally one site) * qty_per_unit
                      status known | partial (some top-level items have no demand rows: lower bound) | unavailable
  available_units     sum over sites of max(0, on_hand - reserved); unavailable if there are no stock rows
  shortage_units      max(0, exposure_units - available_units); None if either side is unavailable
  coverage_ratio      available_units / exposure_units (None if exposure is 0 or unknown)
  eligibility         eligible_for_review | insufficient_evidence | blocked   (NEVER "compliant" or "equivalent")
"""
from __future__ import annotations

from ..db import Conn
from .bom import get_component, where_used
from .compliance import check_compliance
from .inventory import inventory_status
from .suppliers import _supplier_rows

METRICS_VERSION = "1.0"

BLOCKING = {"not_declared_alternate", "supplier_inactive", "lead_time_exceeds_constraint", "compliance_failed"}
GAPS = {"no_supplier_records", "missing_lead_time", "compliance_not_checked", "compliance_unresolved",
        "data_conflict", "stale_evidence"}


def evaluate_candidate(conn: Conn, component_id: str, candidate_id: str, target_market: str | None,
                       max_lead_time_days: int | None, as_of: str) -> dict:
    get_component(conn, component_id)
    get_component(conn, candidate_id)
    reasons: list[str] = []
    declared = conn.query_one("SELECT 1 AS x FROM component_alternates WHERE component_id = ? AND alternate_id = ?",
                              (component_id, candidate_id))
    if declared is None:
        reasons.append("not_declared_alternate")
    sups = _supplier_rows(conn, candidate_id)
    best_lead = None
    usable_ids: list[str] = []
    if not sups:
        reasons.append("no_supplier_records")
    else:
        active = [s for s in sups if s["status"] == "active"]
        if not active:
            reasons.append("supplier_inactive")
        else:
            usable = [s for s in active if s["lead_time_days"] is not None]
            if not usable:
                reasons.append("missing_lead_time")
            else:
                best_lead = min(s["lead_time_days"] for s in usable)
                usable_ids = [s["supplier_id"] for s in usable]
                if max_lead_time_days is not None and best_lead > max_lead_time_days:
                    reasons.append("lead_time_exceeds_constraint")
    comp = check_compliance(conn, candidate_id, target_market, as_of)
    if comp["requirements_modeled"] == 0:
        reasons.append("compliance_not_checked")
    for r in comp["requirements"]:
        if r["status"] == "failed":
            reasons.append("compliance_failed")
        elif r["status"] == "not_checked":
            reasons.append("compliance_not_checked")
        elif r["status"] == "unresolved":
            if "conflicting_records" in r["flags"]:
                reasons.append("data_conflict")
            elif "stale_evidence" in r["flags"]:
                reasons.append("stale_evidence")
            else:
                reasons.append("compliance_unresolved")
    reasons = sorted(set(reasons))
    if any(r in BLOCKING for r in reasons):
        eligibility = "blocked"
    elif reasons:
        eligibility = "insufficient_evidence"
    else:
        eligibility = "eligible_for_review"
    return {"component_id": candidate_id, "eligibility": eligibility, "reasons": reasons,
            "best_lead_time_days": best_lead, "usable_suppliers": usable_ids,
            "compliance_summary": comp["summary"],
            "note": "eligible_for_review means a human should review it; it does not mean compliant or equivalent"}


def impact_metrics(conn: Conn, component_id: str, candidate_id: str | None, target_market: str | None,
                   max_lead_time_days: int | None, site_id: str | None, as_of: str, depth_limit: int = 8) -> dict:
    bom = where_used(conn, component_id, depth_limit)
    exposure_units = 0
    unplanned: list[str] = []
    horizons: set[int] = set()
    for top in bom["top_level"]:
        sql = "SELECT planned_units, horizon_days FROM demand_plan WHERE product_id = ?"
        params: list = [top["product_id"]]
        if site_id:
            sql += " AND site_id = ?"
            params.append(site_id)
        rows = conn.query(sql, params)
        if not rows:
            unplanned.append(top["product_id"])
            continue
        for r in rows:
            exposure_units += int(r["planned_units"]) * top["qty_per_unit"]
            horizons.add(int(r["horizon_days"]))
    if not bom["top_level"]:
        exp_status = "known"
    elif len(unplanned) == len(bom["top_level"]):
        exp_status = "unavailable"
    elif unplanned:
        exp_status = "partial"
    else:
        exp_status = "known"
    exp_units = None if exp_status == "unavailable" else exposure_units
    inv = inventory_status(conn, component_id, site_id)
    avail = None if inv["totals"] is None else inv["totals"]["available"]
    shortage = None if (exp_units is None or avail is None) else max(0, exp_units - avail)
    coverage = None if (exp_units in (None, 0) or avail is None) else round(avail / exp_units, 4)
    cand = None
    if candidate_id:
        cand = evaluate_candidate(conn, component_id, candidate_id, target_market, max_lead_time_days, as_of)
    return {
        "metrics_version": METRICS_VERSION, "component_id": component_id,
        "affected_count": bom["counts"], "affected_ids": bom["affected_ids"],
        "exposure": {"horizon_days": sorted(horizons)[0] if len(horizons) == 1 else (None if not horizons else sorted(horizons)),
                     "units": exp_units, "status": exp_status, "unplanned_products": sorted(unplanned)},
        "inventory": {"available_units": avail, "status": "present" if avail is not None else "unavailable"},
        "shortage_units": shortage, "coverage_ratio": coverage,
        "bom_issues": bom["issues"], "candidate": cand,
    }
