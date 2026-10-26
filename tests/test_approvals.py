import json
import unittest

from changeguard.errors import ApprovalError, PermissionDenied
from tests.helpers import ADMIN, ANALYST, REVIEWER, VIEWER, make_service, submit


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service(test_hooks=True)
        self.ch = submit(self.svc)
        self.svc.investigate(ANALYST, self.ch["change_id"], "baseline")
        self.act = self.svc.approvals.list_actions(self.ch["change_id"])[0]
        self.aid, self.h = self.act["action_id"], self.act["payload_hash"]

    def co_count(self):
        with self.svc.db.connect("reader") as r:
            return r.query_one("SELECT COUNT(*) AS n FROM change_orders")["n"]

    def events(self):
        return [e["event_type"] for e in reversed(self.svc.audit_log(self.ch["change_id"]))]

    def test_nothing_executes_without_approval(self):
        self.assertEqual(self.act["status"], "proposed")
        with self.assertRaises(ApprovalError) as cm:
            self.svc.execute(REVIEWER, self.aid)
        self.assertEqual(cm.exception.kind, "not_approved")
        self.assertEqual(self.co_count(), 0)

    def test_roles_are_enforced_and_denials_audited(self):
        for who in (VIEWER, ANALYST):
            with self.assertRaises(PermissionDenied):
                self.svc.decide(who, self.aid, "approved", self.h)
        self.assertIn("permission_denied", self.events())
        self.svc.decide(REVIEWER, self.aid, "approved", self.h)
        with self.assertRaises(PermissionDenied):
            self.svc.execute(VIEWER, self.aid)
        self.assertEqual(self.co_count(), 0)

    def test_approval_binds_to_exact_payload_hash(self):
        with self.assertRaises(ApprovalError) as cm:
            self.svc.decide(REVIEWER, self.aid, "approved", "0" * 64)
        self.assertEqual(cm.exception.kind, "hash_mismatch")
        self.assertIn("approval_hash_mismatch", self.events())
        self.assertEqual(self.svc.approvals.get_action(self.aid)["status"], "proposed")

    def test_happy_path_executes_once_and_verifies(self):
        self.svc.decide(REVIEWER, self.aid, "approved", self.h, "looks right")
        done = self.svc.execute(REVIEWER, self.aid)
        self.assertEqual(done["status"], "verified")
        self.assertEqual(done["execution"]["verification_status"], "passed")
        self.assertEqual(self.co_count(), 1)
        with self.svc.db.connect("reader") as r:
            co = r.query_one("SELECT * FROM change_orders")
        self.assertEqual(co["status"], "draft")
        self.assertEqual(co["replacement_component_id"], "CMP-109")
        replay = self.svc.execute(REVIEWER, self.aid)  # idempotent
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.co_count(), 1)
        for ev in ("approval_approved", "action_executed", "verification_passed"):
            self.assertIn(ev, self.events())

    def test_rejection_blocks_execution(self):
        self.svc.decide(REVIEWER, self.aid, "rejected", self.h, "not equivalent")
        with self.assertRaises(ApprovalError):
            self.svc.execute(REVIEWER, self.aid)
        with self.assertRaises(ApprovalError) as cm:
            self.svc.decide(REVIEWER, self.aid, "approved", self.h)  # cannot flip a decision
        self.assertEqual(cm.exception.kind, "already_decided")
        self.assertEqual(self.co_count(), 0)

    def test_payload_tampered_after_approval_is_blocked(self):
        self.svc.decide(REVIEWER, self.aid, "approved", self.h)
        tampered = dict(self.act["payload"], replacement_component_id="CMP-106")
        with self.svc.db.connect("admin") as c:  # simulates someone editing the stored payload
            c.execute("UPDATE proposed_actions SET payload_json = ? WHERE action_id = ?", (json.dumps(tampered, sort_keys=True), self.aid))
        with self.assertRaises(ApprovalError) as cm:
            self.svc.execute(REVIEWER, self.aid)
        self.assertEqual(cm.exception.kind, "payload_changed")
        self.assertEqual(self.co_count(), 0)
        self.assertIn("execution_blocked_payload_changed", self.events())

    def test_revision_creates_new_proposal_old_approval_cannot_apply(self):
        new = self.svc.approvals.revise(REVIEWER, self.aid, dict(self.act["payload"], notes="reviewed by Rita"))
        self.assertNotEqual(new["payload_hash"], self.h)
        self.assertEqual(self.svc.approvals.get_action(self.aid)["status"], "superseded")
        with self.assertRaises(ApprovalError):
            self.svc.execute(REVIEWER, self.aid)
        self.svc.decide(REVIEWER, new["action_id"], "approved", new["payload_hash"])
        self.assertEqual(self.svc.execute(REVIEWER, new["action_id"])["status"], "verified")

    def test_revision_cannot_smuggle_in_an_ineligible_candidate(self):
        from changeguard.errors import ToolError
        with self.assertRaises(ToolError):
            self.svc.approvals.revise(REVIEWER, self.aid, dict(self.act["payload"], replacement_component_id="CMP-106"))

    def test_stale_state_blocks_execution(self):
        self.svc.decide(REVIEWER, self.aid, "approved", self.h)
        with self.svc.db.connect("admin") as c:  # world changes after approval: supplier goes inactive
            c.execute("UPDATE suppliers SET status = 'inactive' WHERE supplier_id IN ('S-001','S-002')")
        with self.assertRaises(ApprovalError) as cm:
            self.svc.execute(REVIEWER, self.aid)
        self.assertEqual(cm.exception.kind, "stale_state")
        self.assertEqual(self.co_count(), 0)
        act = self.svc.approvals.get_action(self.aid)
        self.assertEqual(act["status"], "failed")
        self.assertEqual(act["execution"]["outcome"], "failed")

    def test_verification_failure_is_reported_not_hidden(self):
        self.svc.decide(REVIEWER, self.aid, "approved", self.h)
        self.svc.approvals.force_verification_failure = True
        done = self.svc.execute(REVIEWER, self.aid)
        self.assertEqual(done["status"], "verification_failed")
        self.assertEqual(done["execution"]["verification_status"], "failed")
        self.assertIn("verification_failed", self.events())

    def test_stock_hold_before_after(self):
        ch = submit(self.svc, key="hold")
        from changeguard.tools import ToolContext, build_registry
        change = self.svc.get_change(ch["change_id"])
        with self.svc.db.connect("reader") as r:
            reg = build_registry(self.svc.settings)
            ctx = ToolContext(r, self.svc.settings, change, approvals=self.svc.approvals)
            ok, data, _ = reg.call_with_retry("draft_change_request", {"action_type": "RESERVE_STOCK_HOLD", "component_id": "CMP-104",
                                                                       "site_id": "SITE-NORTH", "hold_qty": 250}, ctx)
        self.assertTrue(ok, data)
        act = self.svc.approvals.get_action(data["action_id"])
        self.svc.decide(REVIEWER, act["action_id"], "approved", act["payload_hash"])
        done = self.svc.execute(ADMIN, act["action_id"])
        self.assertEqual(done["status"], "verified")
        self.assertEqual(json.loads(done["execution"]["before_json"]), {"reserved": 100})
        self.assertEqual(json.loads(done["execution"]["after_json"]), {"reserved": 350})
        with self.svc.db.connect("reader") as r:
            self.assertEqual(r.query_one("SELECT reserved FROM inventory WHERE component_id='CMP-104' AND site_id='SITE-NORTH'")["reserved"], 350)

    def test_second_exec_row_is_impossible_at_db_level(self):
        self.svc.decide(REVIEWER, self.aid, "approved", self.h)
        self.svc.execute(REVIEWER, self.aid)
        import sqlite3
        with self.svc.db.connect("admin") as c, self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO executed_actions VALUES ('EXE-X', ?, 'APR-X', 'h', '{}', '{}', 'applied', NULL, 't', 'pending', NULL)", (self.aid,))


if __name__ == "__main__":
    unittest.main()
