import json
import time
import unittest

from changeguard.tools import FaultPlan, ToolContext, build_registry
from changeguard.tools.registry import ToolSpec
from tests.helpers import make_service, submit


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.change = self.svc.get_change(submit(self.svc)["change_id"])
        self.reader = self.svc.db.connect("reader")
        self.reg = build_registry(self.svc.settings)
        self.ctx = ToolContext(self.reader, self.svc.settings, self.change, run_id=None, approvals=self.svc.approvals)

    def call(self, name, args):
        return self.reg.call_with_retry(name, args, self.ctx)

    def test_rejects_bad_ids_and_unknown_fields(self):
        ok, err, _ = self.call("get_bom_impact", {"component_id": "cmp-104"})
        self.assertFalse(ok)
        self.assertEqual(err["error"]["kind"], "invalid_input")
        ok, err, _ = self.call("get_bom_impact", {"component_id": "CMP-104", "evil": 1})
        self.assertEqual(err["error"]["kind"], "invalid_input")
        ok, err, _ = self.call("get_bom_impact", {"component_id": "CMP-104; DROP TABLE x"})
        self.assertFalse(ok)

    def test_not_found_is_not_retried(self):
        ok, err, attempts = self.call("get_bom_impact", {"component_id": "CMP-999"})
        self.assertEqual((ok, err["error"]["kind"], attempts), (False, "not_found", 1))

    def test_model_cannot_override_constraints(self):
        ok, err, _ = self.call("calculate_impact_metrics", {"component_id": "CMP-104", "candidate_component_id": "CMP-106", "target_market": "US"})
        self.assertFalse(ok)  # unknown field rejected
        ok, data, _ = self.call("calculate_impact_metrics", {"component_id": "CMP-104", "candidate_component_id": "CMP-106"})
        self.assertEqual(data["candidate"]["eligibility"], "blocked")  # market EU comes from the request
        self.assertEqual(data["constraints_applied"]["target_market"], "EU")

    def test_transient_faults_recover(self):
        for fault in ("timeout", "temporary_db", "empty", "malformed"):
            self.reg.faults = FaultPlan({"get_inventory_status": [fault]})
            ok, data, attempts = self.call("get_inventory_status", {"component_id": "CMP-104"})
            self.assertTrue(ok, fault)
            self.assertEqual(attempts, 2, fault)
            self.assertEqual(data["totals"]["available"], 1400)

    def test_persistent_fault_hits_retry_limit(self):
        self.reg.faults = FaultPlan({"get_inventory_status": ["timeout"] * 10})
        ok, err, attempts = self.call("get_inventory_status", {"component_id": "CMP-104"})
        self.assertFalse(ok)
        self.assertEqual(err["error"]["kind"], "retry_limit_exceeded")
        self.assertEqual(attempts, 1 + self.svc.settings.max_retries_per_tool)

    def test_permanent_fault_not_retried(self):
        self.reg.faults = FaultPlan({"get_inventory_status": ["permanent"] * 3})
        ok, err, attempts = self.call("get_inventory_status", {"component_id": "CMP-104"})
        self.assertEqual((ok, err["error"]["kind"], attempts), (False, "tool_failure", 1))

    def test_real_timeout(self):
        self.svc.settings.tool_timeout_seconds = 0.05
        self.svc.settings.max_retries_per_tool = 0
        self.reg.register(ToolSpec("slow", "slow", {"type": "object", "properties": {}, "additionalProperties": False},
                                   lambda c, a: (time.sleep(0.3), {"x": 1})[1], "read"))
        ok, err, _ = self.call("slow", {})
        self.assertFalse(ok)
        self.assertIn("timeout", json.dumps(err))

    def test_write_tool_never_exposed_or_runnable(self):
        self.assertNotIn("apply_approved_sandbox_change", [t["name"] for t in self.reg.llm_specs()])
        self.assertFalse(self.reg.is_exposed("apply_approved_sandbox_change"))
        ok, err, _ = self.call("apply_approved_sandbox_change", {"action_id": "ACT-1"})
        self.assertEqual(err["error"]["kind"], "permission_denied")

    def test_draft_requires_eligible_candidate(self):
        bad = {"action_type": "CREATE_CHANGE_ORDER", "component_id": "CMP-104", "replacement_component_id": "CMP-106",
               "disposition": "substitute_candidate_review"}
        ok, err, _ = self.call("draft_change_request", bad)
        self.assertEqual((ok, err["error"]["kind"]), (False, "unsafe_proposal"))
        self.assertIn("blocked", err["error"]["message"])
        for cand in ("CMP-107", "CMP-105"):  # insufficient evidence is also not draftable
            ok, err, _ = self.call("draft_change_request", {**bad, "replacement_component_id": cand})
            self.assertFalse(ok, cand)
        self.assertEqual(self.svc.approvals.list_actions(self.change["change_id"]), [])

    def test_draft_forces_own_change_and_component(self):
        ok, err, _ = self.call("draft_change_request", {"action_type": "CREATE_CHANGE_ORDER", "component_id": "CMP-210",
                                                        "disposition": "redesign_review"})
        self.assertEqual(err["error"]["kind"], "unsafe_proposal")
        ok, data, _ = self.call("draft_change_request", {"action_type": "CREATE_CHANGE_ORDER", "component_id": "CMP-104",
                                                         "replacement_component_id": "CMP-109", "disposition": "substitute_candidate_review"})
        self.assertTrue(ok)
        self.assertTrue(data["requires_human_approval"])
        act = self.svc.approvals.get_action(data["action_id"])
        self.assertEqual(act["payload"]["change_id"], self.change["change_id"])
        self.assertEqual(act["status"], "proposed")

    def test_proposal_count_is_limited(self):
        self.ctx.max_proposals = 1
        a = {"action_type": "CREATE_CHANGE_ORDER", "component_id": "CMP-104", "replacement_component_id": "CMP-109",
             "disposition": "substitute_candidate_review"}
        self.assertTrue(self.call("draft_change_request", a)[0])
        ok, err, _ = self.call("draft_change_request", {**a, "disposition": "last_time_buy", "replacement_component_id": None})
        self.assertFalse(ok)
        self.assertEqual(err["error"]["kind"], "permission_denied")

    def test_stock_hold_rules(self):
        base = {"action_type": "RESERVE_STOCK_HOLD", "component_id": "CMP-104", "site_id": "SITE-NORTH", "hold_qty": 10_000}
        ok, err, _ = self.call("draft_change_request", base)
        self.assertEqual(err["error"]["kind"], "unsafe_proposal")  # exceeds available
        ok, data, _ = self.call("draft_change_request", {**base, "hold_qty": 100})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
