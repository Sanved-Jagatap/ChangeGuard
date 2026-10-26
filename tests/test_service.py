import unittest

from changeguard.errors import BudgetExceeded, PermissionDenied, ValidationError
from changeguard.llm.mock import MockPolicyLLM
from tests.helpers import ADMIN, ANALYST, REVIEWER, VIEWER, make_service, submit


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def test_intake_validation(self):
        bad = [{}, {"component_id": "CMP-104"}, {"component_id": "x", "change_type": "discontinued", "description": "valid text here"},
               {"component_id": "CMP-104", "change_type": "nope", "description": "valid text here"},
               {"component_id": "CMP-104", "change_type": "discontinued", "description": "hi"},
               {"component_id": "CMP-104", "change_type": "discontinued", "description": "valid text here", "extra": 1},
               {"component_id": "CMP-104", "change_type": "discontinued", "description": "valid text here", "target_market": "MARS"},
               {"component_id": "CMP-104", "change_type": "discontinued", "description": "valid text here", "constraints": {"max_lead_time_days": 0}},
               {"component_id": "CMP-104", "change_type": "discontinued", "description": "x" * 2000}]
        for i, b in enumerate(bad):
            with self.assertRaises(ValidationError, msg=str(b)[:60]):
                self.svc.submit_change(ANALYST, b, f"bad-{i}")

    def test_unknown_component_and_site(self):
        with self.assertRaises(ValidationError):
            submit(self.svc, component="CMP-999")
        with self.assertRaises(ValidationError):
            submit(self.svc, constraints={"site_id": "SITE-MARS"}, key="s")

    def test_viewer_cannot_submit_or_investigate(self):
        with self.assertRaises(PermissionDenied):
            self.svc.submit_change(VIEWER, {"component_id": "CMP-104", "change_type": "discontinued", "description": "valid text"}, "v1")
        ch = submit(self.svc)
        with self.assertRaises(PermissionDenied):
            self.svc.investigate(VIEWER, ch["change_id"], "baseline")

    def test_idempotent_intake(self):
        a = submit(self.svc, key="same")
        b = submit(self.svc, key="same")
        self.assertEqual(a["change_id"], b["change_id"])
        self.assertTrue(b["idempotent_replay"])
        self.assertEqual(len(self.svc.list_changes()), 1)

    def test_run_persisted_with_trace_and_audit(self):
        ch = submit(self.svc)
        res = self.svc.investigate(ANALYST, ch["change_id"], "agent")
        run = self.svc.get_run(res.run_id)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["report"]["outcome"], "recommendation")
        steps = self.svc.run_steps(res.run_id)
        self.assertEqual(len(steps), run["step_count"])
        self.assertTrue(any(s["name"] == "calculate_impact_metrics" and s["output"] for s in steps))
        events = [e["event_type"] for e in self.svc.audit_log(ch["change_id"])]
        self.assertIn("run_started", events)
        self.assertIn("run_finished", events)
        self.assertEqual(self.svc.get_change(ch["change_id"])["status"], "investigated")

    def test_daily_budget_blocks_hosted_runs_only(self):
        class Hosted(MockPolicyLLM):
            provider = "gemini"
        self.svc.llm_factory = lambda s, p, m: Hosted()
        self.svc.settings.max_agent_runs_per_day = 1
        ch = submit(self.svc)
        self.svc.investigate(ANALYST, ch["change_id"], "agent")
        with self.assertRaises(BudgetExceeded):
            self.svc.investigate(ANALYST, ch["change_id"], "agent")
        self.svc.investigate(ANALYST, ch["change_id"], "baseline")  # baseline is free

    def test_reset_is_guarded(self):
        with self.assertRaises(PermissionDenied):
            self.svc.reset_demo(REVIEWER)
        with self.assertRaises(PermissionDenied):
            self.svc.reset_demo(ADMIN)  # CG_ALLOW_RESET not enabled
        self.svc.settings.allow_reset = True
        submit(self.svc)
        self.svc.reset_demo(ADMIN)
        self.assertEqual(self.svc.list_changes(), [])


if __name__ == "__main__":
    unittest.main()
