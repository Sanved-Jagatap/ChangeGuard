import sqlite3
import unittest

from tests.helpers import make_service


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.db = self.svc.db

    def denied(self, role, sql, params=()):
        with self.db.connect(role) as c, self.assertRaises(sqlite3.Error):
            c.execute(sql, params)

    def test_reader_cannot_write(self):
        self.denied("reader", "UPDATE inventory SET reserved = 0")
        self.denied("reader", "INSERT INTO sites VALUES ('X','Y')")

    def test_reader_cannot_do_ddl_or_pragma(self):
        self.denied("reader", "DROP TABLE components")
        self.denied("reader", "PRAGMA journal_mode=WAL")

    def test_recorder_cannot_touch_business_data(self):
        self.denied("recorder", "UPDATE inventory SET reserved = 5")
        self.denied("recorder", "INSERT INTO change_orders(co_id) VALUES ('x')")
        self.denied("recorder", "DELETE FROM bom_links")

    def test_sandbox_writer_is_narrow(self):
        self.denied("sandbox_writer", "DELETE FROM inventory")
        self.denied("sandbox_writer", "UPDATE components SET description = 'x'")
        self.denied("sandbox_writer", "INSERT INTO approvals VALUES ('a','b','c','approved','r','reviewer',NULL,'t')")
        self.denied("sandbox_writer", "DROP TABLE inventory")

    def test_audit_is_append_only_even_for_admin(self):
        with self.db.connect("recorder") as c:
            c.execute("INSERT INTO audit_events(ts, actor, event_type, detail_json) VALUES ('t','a','e','{}')")
        with self.db.connect("admin") as c:
            with self.assertRaises(sqlite3.Error):
                c.execute("UPDATE audit_events SET actor = 'evil'")
            with self.assertRaises(sqlite3.Error):
                c.execute("DELETE FROM audit_events")

    def test_approvals_are_immutable(self):
        from tests.helpers import REVIEWER, submit
        ch = submit(self.svc)
        self.svc.investigate(REVIEWER, ch["change_id"], "baseline")
        act = self.svc.approvals.list_actions(ch["change_id"])[0]
        self.svc.decide(REVIEWER, act["action_id"], "rejected", act["payload_hash"], "not now")
        with self.db.connect("admin") as c:
            with self.assertRaises(sqlite3.Error):
                c.execute("UPDATE approvals SET decision = 'approved'")
            with self.assertRaises(sqlite3.Error):
                c.execute("DELETE FROM approvals")

    def test_constraints_reject_bad_data(self):
        with self.db.connect("admin") as c:
            with self.assertRaises(sqlite3.Error):
                c.execute("UPDATE inventory SET on_hand = -1")
            with self.assertRaises(sqlite3.Error):
                c.execute("INSERT INTO bom_links VALUES ('CMP-104','CMP-104',1)")
            with self.assertRaises(sqlite3.Error):
                c.execute("INSERT INTO bom_links VALUES ('ASM-MCB1','CMP-210',0)")


if __name__ == "__main__":
    unittest.main()
