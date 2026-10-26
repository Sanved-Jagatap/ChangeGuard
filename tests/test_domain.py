import json
import unittest

from changeguard.domain import bom, compliance, docs, inventory, metrics, quality
from changeguard.errors import ToolError
from changeguard.seed import AS_OF, build_seed
from tests.helpers import make_service


class DomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = make_service()
        cls.r = cls.svc.db.connect("reader")

    def test_where_used_multilevel_quantities(self):
        res = bom.where_used(self.r, "CMP-104")
        self.assertEqual(res["affected_ids"], ["ASM-DRV1", "ASM-MCB1", "ASM-PWR1", "ASM-PWR2", "PRD-EB100", "PRD-EB200", "PRD-SP01"])
        per = {t["product_id"]: t["qty_per_unit"] for t in res["top_level"]}
        self.assertEqual(per, {"PRD-EB100": 3, "PRD-EB200": 6, "PRD-SP01": 3})
        self.assertEqual(res["counts"], {"assemblies": 4, "products": 3, "total": 7})

    def test_shared_part_has_wide_impact(self):
        res = bom.where_used(self.r, "CMP-410")
        self.assertEqual({t["product_id"]: t["qty_per_unit"] for t in res["top_level"]},
                         {"PRD-CH10": 1, "PRD-EB100": 4, "PRD-EB200": 7, "PRD-SP01": 2})

    def test_depth_limit_truncates(self):
        res = bom.where_used(self.r, "CMP-104", depth_limit=2)
        self.assertTrue(res["truncated"])
        self.assertNotIn("PRD-EB100", res["affected_ids"])

    def test_unknown_component(self):
        with self.assertRaises(ToolError) as cm:
            bom.where_used(self.r, "CMP-999")
        self.assertEqual(cm.exception.kind, "not_found")

    def test_cycle_is_reported_not_looped(self):
        seed = build_seed()
        seed.bom.append({"parent_id": "CMP-104", "child_id": "PRD-EB100", "qty_per": 1})  # creates a cycle
        svc = make_service(seed)
        with svc.db.connect("reader") as r:
            res = bom.where_used(r, "CMP-104")
            self.assertTrue(any(i["code"] == "cycle_detected" for i in res["issues"]))
            self.assertTrue(any(f["check"] == "bom_cycle" for f in quality.run_checks(r, AS_OF)))

    def test_inventory_anomaly_and_unavailable(self):
        inv = inventory.inventory_status(self.r, "CMP-411")
        self.assertTrue(any(s["anomaly"] for s in inv["sites"]))
        self.assertEqual(next(s for s in inv["sites"] if s["site_id"] == "SITE-SOUTH")["available"], 0)
        none = inventory.inventory_status(self.r, "CMP-511")
        self.assertEqual(none["stock_data"], "unavailable")
        self.assertIsNone(none["totals"])

    def test_compliance_statuses(self):
        c = {x["requirement_id"]: x for x in compliance.check_compliance(self.r, "CMP-105", "EU", AS_OF)["requirements"]}
        self.assertEqual(c["REQ-EU-ROHS"]["status"], "confirmed")
        self.assertEqual(c["REQ-EU-REACH"]["status"], "unresolved")
        us = {x["requirement_id"]: x for x in compliance.check_compliance(self.r, "CMP-105", "US", AS_OF)["requirements"]}
        self.assertEqual(us["REQ-US-P65"]["status"], "unresolved")
        self.assertIn("stale_evidence", us["REQ-US-P65"]["flags"])
        conf = {x["requirement_id"]: x for x in compliance.check_compliance(self.r, "CMP-212", "EU", AS_OF)["requirements"]}
        self.assertIn("conflicting_records", conf["REQ-EU-ROHS"]["flags"])
        nc = {x["requirement_id"]: x for x in compliance.check_compliance(self.r, "CMP-411", "EU", AS_OF)["requirements"]}
        self.assertEqual(nc["REQ-EU-REACH"]["status"], "not_checked")
        failed = {x["requirement_id"]: x for x in compliance.check_compliance(self.r, "CMP-106", "EU", AS_OF)["requirements"]}
        self.assertEqual(failed["REQ-EU-ROHS"]["status"], "failed")

    def test_metrics_cmp104(self):
        m = metrics.impact_metrics(self.r, "CMP-104", None, "EU", None, None, AS_OF)
        self.assertEqual((m["exposure"]["units"], m["inventory"]["available_units"], m["shortage_units"]), (3600, 1400, 2200))
        self.assertEqual(m["coverage_ratio"], 0.3889)

    def test_metrics_other_components(self):
        for cid, exp, avail, short in (("CMP-310", 2250, 1000, 1250), ("CMP-410", 4800, 14000, 0), ("CMP-510", 2100, 1000, 1100)):
            m = metrics.impact_metrics(self.r, cid, None, None, None, None, AS_OF)
            self.assertEqual((m["exposure"]["units"], m["inventory"]["available_units"], m["shortage_units"]), (exp, avail, short), cid)

    def test_site_filter(self):
        m = metrics.impact_metrics(self.r, "CMP-104", None, "EU", None, "SITE-NORTH", AS_OF)
        self.assertEqual(m["exposure"]["units"], (400 * 3) + (120 * 6) + (100 * 3))
        self.assertEqual(m["inventory"]["available_units"], 800)

    def test_candidate_matrix(self):
        expected = {
            ("CMP-105", "EU"): "insufficient_evidence", ("CMP-105", "US"): "insufficient_evidence",
            ("CMP-106", "EU"): "blocked", ("CMP-106", "US"): "eligible_for_review",
            ("CMP-107", "EU"): "insufficient_evidence", ("CMP-108", "EU"): "blocked",
            ("CMP-109", "EU"): "eligible_for_review", ("CMP-109", "US"): "eligible_for_review",
        }
        for (cand, mk), want in expected.items():
            got = metrics.evaluate_candidate(self.r, "CMP-104", cand, mk, None, AS_OF)["eligibility"]
            self.assertEqual(got, want, (cand, mk))

    def test_lead_time_constraint_blocks(self):
        e = metrics.evaluate_candidate(self.r, "CMP-104", "CMP-109", "EU", 20, AS_OF)
        self.assertEqual(e["eligibility"], "blocked")
        self.assertIn("lead_time_exceeds_constraint", e["reasons"])

    def test_undeclared_alternate_is_blocked(self):
        e = metrics.evaluate_candidate(self.r, "CMP-104", "CMP-211", "EU", None, AS_OF)
        self.assertIn("not_declared_alternate", e["reasons"])
        self.assertEqual(e["eligibility"], "blocked")

    def test_missing_demand_is_partial_not_zero(self):
        seed = build_seed()
        seed.demand = [d for d in seed.demand if d["product_id"] != "PRD-EB200"]
        svc = make_service(seed)
        with svc.db.connect("reader") as r:
            m = metrics.impact_metrics(r, "CMP-104", None, "EU", None, None, AS_OF)
        self.assertEqual(m["exposure"]["status"], "partial")
        self.assertEqual(m["exposure"]["unplanned_products"], ["PRD-EB200"])
        self.assertEqual(m["exposure"]["units"], 1950 + 450)

    def test_docs_flag_injection(self):
        res = docs.search_documents(self.r, "CMP-106 replacement bulletin")
        flagged = [d for d in res["results"] if d["flags"]]
        self.assertTrue(flagged and flagged[0]["doc_id"] == "DOC-005")
        clean = docs.search_documents(self.r, "connector derating")
        self.assertFalse(any(d["flags"] for d in clean["results"]))

    def test_data_quality_findings(self):
        found = {(f["check"], f["subject"]) for f in quality.run_checks(self.r, AS_OF)}
        self.assertIn(("impossible_inventory", "CMP-411@SITE-SOUTH"), found)
        self.assertIn(("stale_compliance", "CMP-105/REQ-US-P65"), found)
        self.assertIn(("conflicting_compliance", "CMP-212/REQ-EU-ROHS"), found)
        self.assertIn(("missing_lead_time", "CMP-107/S-001"), found)

    def test_results_are_json_serialisable(self):
        json.dumps(metrics.impact_metrics(self.r, "CMP-104", "CMP-109", "EU", None, None, AS_OF))


if __name__ == "__main__":
    unittest.main()
