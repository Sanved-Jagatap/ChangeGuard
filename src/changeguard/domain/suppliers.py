"""Supplier options and declared candidate alternates."""
from __future__ import annotations

from ..db import Conn
from .bom import get_component


def _supplier_rows(conn: Conn, component_id: str) -> list[dict]:
    rows = conn.query(
        "SELECT cs.supplier_id, s.name, s.status, s.risk_level, cs.lead_time_days, cs.unit_cost_cents, cs.is_preferred "
        "FROM component_suppliers cs JOIN suppliers s ON s.supplier_id = cs.supplier_id "
        "WHERE cs.component_id = ? ORDER BY cs.supplier_id", (component_id,))
    out = []
    for r in rows:
        lt = r["lead_time_days"]
        out.append({
            "supplier_id": r["supplier_id"], "name": r["name"], "status": r["status"], "risk_level": r["risk_level"],
            "lead_time_days": None if lt is None else int(lt),
            "lead_time_data": "unavailable" if lt is None else "present",
            "unit_cost_cents": r["unit_cost_cents"], "is_preferred": bool(r["is_preferred"]),
            "usable": r["status"] == "active" and lt is not None,
        })
    return out


def supplier_options(conn: Conn, component_id: str) -> dict:
    comp = get_component(conn, component_id)
    sups = _supplier_rows(conn, component_id)
    issues = []
    if not sups:
        issues.append({"code": "no_supplier_records", "detail": f"{component_id} has no supplier records"})
    elif sum(1 for s in sups if s["status"] == "active") == 1 and len(sups) == 1:
        issues.append({"code": "single_source", "detail": f"{component_id} has a single supplier"})
    for s in sups:
        if s["lead_time_days"] is None:
            issues.append({"code": "missing_lead_time", "detail": f"{component_id}/{s['supplier_id']}: lead time not recorded"})
    alts = []
    for a in conn.query("SELECT alternate_id FROM component_alternates WHERE component_id = ? ORDER BY alternate_id", (component_id,)):
        ac = get_component(conn, a["alternate_id"])
        alts.append({"component_id": ac["component_id"], "description": ac["description"],
                     "lifecycle_status": ac["lifecycle_status"], "relationship": "declared_candidate",
                     "note": "candidate only: engineering equivalence is not established by this system",
                     "suppliers": _supplier_rows(conn, ac["component_id"])})
    if not alts:
        issues.append({"code": "no_alternates", "detail": f"{component_id} has no declared candidate alternates"})
    return {"component_id": component_id, "lifecycle_status": comp["lifecycle_status"], "suppliers": sups,
            "alternates": alts, "issues": issues}
