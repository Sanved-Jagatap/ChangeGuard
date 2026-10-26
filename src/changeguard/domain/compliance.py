"""Compliance evidence lookup. 'Unresolved', 'not_checked', 'stale' and 'conflicting' are never 'compliant'."""
from __future__ import annotations

from datetime import date, timedelta

from ..db import Conn
from .bom import get_component

STALE_DAYS = 730  # evidence older than 24 months must be refreshed


def _is_stale(evidence_date: str | None, as_of: str) -> bool:
    if not evidence_date:
        return True
    return date.fromisoformat(evidence_date) < date.fromisoformat(as_of) - timedelta(days=STALE_DAYS)


def check_compliance(conn: Conn, component_id: str, target_market: str | None, as_of: str) -> dict:
    get_component(conn, component_id)
    sql = "SELECT requirement_id, market, regulation FROM compliance_requirements"
    params: list = []
    if target_market:
        sql += " WHERE market = ?"
        params.append(target_market)
    reqs = conn.query(sql + " ORDER BY requirement_id", params)
    out, summary = [], {"confirmed": 0, "failed": 0, "unresolved": 0, "not_checked": 0}
    for rq in reqs:
        recs = conn.query(
            "SELECT record_id, status, evidence_ref, evidence_date, note FROM compliance_records "
            "WHERE component_id = ? AND requirement_id = ? ORDER BY record_id", (component_id, rq["requirement_id"]))
        flags: list[str] = []
        statuses = {r["status"] for r in recs}
        if not recs:
            status, reason = "not_checked", "no evidence record exists"
        elif statuses == {"failed"}:
            status, reason = "failed", "evidence shows a failed requirement"
        elif statuses == {"confirmed"}:
            newest = max((r["evidence_date"] or "") for r in recs) or None
            if _is_stale(newest, as_of):
                status, reason = "unresolved", f"evidence is older than {STALE_DAYS} days or undated"
                flags.append("stale_evidence")
            else:
                status, reason = "confirmed", "current evidence confirms the requirement"
        elif "failed" in statuses and "confirmed" in statuses:
            status, reason = "unresolved", "conflicting evidence records (confirmed and failed)"
            flags.append("conflicting_records")
        else:
            status, reason = "unresolved", "evidence is incomplete or pending"
        summary[status] += 1
        out.append({"requirement_id": rq["requirement_id"], "market": rq["market"], "regulation": rq["regulation"],
                    "status": status, "reason": reason, "flags": flags,
                    "records": [{k: r[k] for k in ("record_id", "status", "evidence_ref", "evidence_date")} for r in recs]})
    return {"component_id": component_id, "target_market": target_market or "all", "as_of": as_of,
            "requirements": out, "summary": summary,
            "requirements_modeled": len(out),
            "note": "Only 'confirmed' means evidence is present. Unresolved/not_checked/stale/conflicting are NOT compliant."}
