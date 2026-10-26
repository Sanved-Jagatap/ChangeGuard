"""Inventory lookups. Missing stock rows are reported as unavailable, never as zero."""
from __future__ import annotations

from ..db import Conn
from ..errors import ToolError
from .bom import get_component


def inventory_status(conn: Conn, component_id: str, site_id: str | None = None) -> dict:
    comp = get_component(conn, component_id)
    if site_id is not None and conn.query_one("SELECT 1 AS x FROM sites WHERE site_id = ?", (site_id,)) is None:
        raise ToolError("not_found", f"unknown site_id {site_id}")
    sql = "SELECT site_id, on_hand, reserved FROM inventory WHERE component_id = ?"
    params: list = [component_id]
    if site_id:
        sql += " AND site_id = ?"
        params.append(site_id)
    rows = conn.query(sql + " ORDER BY site_id", params)
    sites, issues = [], []
    for r in rows:
        on_hand, reserved = int(r["on_hand"]), int(r["reserved"])
        anomaly = "reserved_exceeds_on_hand" if reserved > on_hand else None
        if anomaly:
            issues.append({"code": "data_conflict", "detail": f"{component_id} at {r['site_id']}: reserved {reserved} > on hand {on_hand}"})
        sites.append({"site_id": r["site_id"], "on_hand": on_hand, "reserved": reserved,
                      "available": max(0, on_hand - reserved), "anomaly": anomaly})
    if not sites:
        return {"component_id": component_id, "uom": comp["uom"], "stock_data": "unavailable", "sites": [],
                "totals": None, "issues": [{"code": "stock_unavailable", "detail": "no inventory rows"}]}
    return {"component_id": component_id, "uom": comp["uom"], "stock_data": "present", "sites": sites,
            "totals": {"on_hand": sum(s["on_hand"] for s in sites), "reserved": sum(s["reserved"] for s in sites),
                       "available": sum(s["available"] for s in sites)},
            "issues": issues}
