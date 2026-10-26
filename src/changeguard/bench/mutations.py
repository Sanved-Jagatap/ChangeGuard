"""Seeded data mutations used to build benchmark scenarios (applied to a copy of SeedData)."""
from __future__ import annotations

from ..seed import SeedData


def _comp(seed: SeedData, cid: str) -> dict:
    return next(c for c in seed.components if c["component_id"] == cid)


def apply_mutations(seed: SeedData, muts: list[dict]) -> SeedData:
    for m in muts:
        op = m["op"]
        if op == "set_inventory":
            rows = [r for r in seed.inventory if r["component_id"] == m["component_id"] and r["site_id"] == m["site_id"]]
            if rows:
                rows[0].update(on_hand=m["on_hand"], reserved=m["reserved"])
            else:
                seed.inventory.append({"component_id": m["component_id"], "site_id": m["site_id"], "on_hand": m["on_hand"], "reserved": m["reserved"]})
        elif op == "delete_inventory":
            seed.inventory = [r for r in seed.inventory if r["component_id"] != m["component_id"]]
        elif op == "set_supplier_status":
            next(s for s in seed.suppliers if s["supplier_id"] == m["supplier_id"])["status"] = m["status"]
        elif op == "set_lead_time":
            for r in seed.component_suppliers:
                if r["component_id"] == m["component_id"] and (m.get("supplier_id") in (None, r["supplier_id"])):
                    r["lead_time_days"] = m["days"]
        elif op == "delete_component_suppliers":
            seed.component_suppliers = [r for r in seed.component_suppliers if r["component_id"] != m["component_id"]]
        elif op == "set_compliance":
            seed.records = [r for r in seed.records if not (r["component_id"] == m["component_id"] and r["requirement_id"] == m["requirement_id"])]
            n = len(seed.records) + 1000
            seed.records.append({"record_id": f"M-{n}", "component_id": m["component_id"], "requirement_id": m["requirement_id"],
                                 "status": m["status"], "evidence_ref": "EV-M", "evidence_date": m.get("date", "2026-09-01"), "note": "bench"})
        elif op == "add_compliance_record":
            n = len(seed.records) + 2000
            seed.records.append({"record_id": f"M-{n}", "component_id": m["component_id"], "requirement_id": m["requirement_id"],
                                 "status": m["status"], "evidence_ref": "EV-M", "evidence_date": m.get("date", "2026-09-01"), "note": "bench"})
        elif op == "delete_compliance":
            seed.records = [r for r in seed.records if not (r["component_id"] == m["component_id"] and
                                                            m.get("requirement_id") in (None, r["requirement_id"]))]
        elif op == "insert_level":
            link = next(b for b in seed.bom if b["parent_id"] == m["parent"] and b["child_id"] == m["child"])
            seed.components.append({"component_id": m["new_id"], "description": "Inserted sub-assembly (bench)", "kind": "assembly",
                                    "lifecycle_status": "active", "criticality": None})
            q = link["qty_per"]
            link["child_id"] = m["new_id"]
            seed.bom.append({"parent_id": m["new_id"], "child_id": m["child"], "qty_per": 1})
            link["qty_per"] = q
        elif op == "add_bom_link":
            seed.bom.append({"parent_id": m["parent"], "child_id": m["child"], "qty_per": m.get("qty", 1)})
        elif op == "delete_demand":
            pid = m.get("product_id")
            seed.demand = [d for d in seed.demand if pid is not None and d["product_id"] != pid]
        elif op == "delete_alternates":
            seed.alternates = [a for a in seed.alternates if a[0] != m["component_id"]]
        elif op == "keep_alternates":
            seed.alternates = [a for a in seed.alternates if a[0] != m["component_id"] or a[1] in m["keep"]]
        elif op == "set_description":
            _comp(seed, m["component_id"])["description"] = m["text"]
        elif op == "add_document":
            seed.documents.append({"doc_id": m["doc_id"], "title": m["title"], "doc_type": m.get("doc_type", "supplier_notice"), "body": m["body"]})
        else:
            raise ValueError(f"unknown mutation {op}")
    return seed
