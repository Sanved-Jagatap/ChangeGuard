"""Data-quality checks (FR-12). Run against any database; returns a list of findings."""
from __future__ import annotations

import re

from ..db import Conn
from .compliance import check_compliance


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("ltd", "").replace("inc", "").replace("gmbh", ""))


def run_checks(conn: Conn, as_of: str) -> list[dict]:
    f: list[dict] = []

    def add(check, severity, subject, detail):
        f.append({"check": check, "severity": severity, "subject": subject, "detail": detail})

    for r in conn.query("SELECT b.parent_id, b.child_id FROM bom_links b LEFT JOIN components p ON p.component_id=b.parent_id "
                        "LEFT JOIN components c ON c.component_id=b.child_id WHERE p.component_id IS NULL OR c.component_id IS NULL"):
        add("orphan_bom_link", "error", f"{r['parent_id']}->{r['child_id']}", "BOM link references a missing component")
    # cycles
    children: dict[str, list[str]] = {}
    for r in conn.query("SELECT parent_id, child_id FROM bom_links"):
        children.setdefault(r["parent_id"], []).append(r["child_id"])
    state: dict[str, int] = {}

    def dfs(n: str, trail: list[str]) -> None:
        state[n] = 1
        for c in children.get(n, []):
            if state.get(c) == 1:
                add("bom_cycle", "error", c, "cycle: " + " -> ".join(trail + [n, c]))
            elif state.get(c) is None:
                dfs(c, trail + [n])
        state[n] = 2

    for n in list(children):
        if state.get(n) is None:
            dfs(n, [])
    for r in conn.query("SELECT component_id, site_id, on_hand, reserved FROM inventory WHERE reserved > on_hand"):
        add("impossible_inventory", "error", f"{r['component_id']}@{r['site_id']}",
            f"reserved {r['reserved']} exceeds on hand {r['on_hand']}")
    for r in conn.query("SELECT component_id FROM components WHERE kind='purchased' AND component_id NOT IN "
                        "(SELECT component_id FROM component_suppliers)"):
        add("no_supplier", "warning", r["component_id"], "purchased component has no supplier records")
    for r in conn.query("SELECT component_id, supplier_id FROM component_suppliers WHERE lead_time_days IS NULL"):
        add("missing_lead_time", "warning", f"{r['component_id']}/{r['supplier_id']}", "lead time not recorded")
    names: dict[tuple, list[str]] = {}
    for r in conn.query("SELECT cs.component_id, s.supplier_id, s.name FROM component_suppliers cs "
                        "JOIN suppliers s ON s.supplier_id = cs.supplier_id"):
        names.setdefault((r["component_id"], _norm(r["name"])), []).append(r["supplier_id"])
    for (comp, _), ids in names.items():
        if len(ids) > 1:
            add("duplicate_supplier_mapping", "warning", comp, f"same supplier appears as {sorted(ids)}")
    for r in conn.query("SELECT DISTINCT component_id FROM compliance_records"):
        res = check_compliance(conn, r["component_id"], None, as_of)
        for q in res["requirements"]:
            if "stale_evidence" in q["flags"]:
                add("stale_compliance", "warning", f"{r['component_id']}/{q['requirement_id']}", q["reason"])
            if "conflicting_records" in q["flags"]:
                add("conflicting_compliance", "error", f"{r['component_id']}/{q['requirement_id']}", q["reason"])
    for r in conn.query("SELECT DISTINCT c.component_id FROM components c WHERE c.kind='product' AND c.component_id NOT IN "
                        "(SELECT product_id FROM demand_plan)"):
        add("no_demand_plan", "warning", r["component_id"], "product has no demand plan rows")
    return sorted(f, key=lambda x: (x["check"], x["subject"]))
