"""Optional integration test against a real PostgreSQL. Skipped unless CG_TEST_POSTGRES_URL is set.

    docker compose up -d db
    set CG_TEST_POSTGRES_URL=postgresql://cg_admin:cg_admin_dev@localhost:5434/changeguard   (PowerShell: $env:CG_TEST_POSTGRES_URL=...)
    python -m pytest tests/test_postgres.py -q

It DROPS the public schema of that database. Use a throwaway database."""
import os
import unittest

from tests.helpers import ANALYST, REVIEWER, submit

URL = os.environ.get("CG_TEST_POSTGRES_URL")


@unittest.skipUnless(URL, "CG_TEST_POSTGRES_URL not set")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from changeguard.config import Settings
        from changeguard.db import Database
        from changeguard.seed import setup_database
        from changeguard.service import ChangeGuardService
        cls.db = Database(URL)
        cls.db.drop_all()
        setup_database(cls.db)
        cls.db.apply_postgres_roles({"reader": "cg_reader_dev", "recorder": "cg_recorder_dev", "sandbox": "cg_sandbox_dev"})
        base = URL.split("@", 1)[1]
        cls.db = Database(URL, f"postgresql://cg_reader:cg_reader_dev@{base}", f"postgresql://cg_recorder:cg_recorder_dev@{base}",
                          f"postgresql://cg_sandbox:cg_sandbox_dev@{base}")
        s = Settings()
        s.retry_backoff_seconds = 0.0
        cls.svc = ChangeGuardService(cls.db, s)

    def test_end_to_end_with_least_privilege_roles(self):
        ch = submit(self.svc)
        res = self.svc.investigate(ANALYST, ch["change_id"], "baseline")
        self.assertEqual(res.report["recommended_component_id"], "CMP-109")
        self.assertEqual(res.report["key_figures"]["shortage_units"], 2200)
        act = self.svc.approvals.list_actions(ch["change_id"])[0]
        self.svc.decide(REVIEWER, act["action_id"], "approved", act["payload_hash"])
        self.assertEqual(self.svc.execute(REVIEWER, act["action_id"])["status"], "verified")

    def test_roles_are_really_restricted(self):
        import psycopg
        with self.db.connect("reader") as c, self.assertRaises(psycopg.Error):
            c.execute("UPDATE inventory SET reserved = 0")
        with self.db.connect("recorder") as c, self.assertRaises(psycopg.Error):
            c.execute("UPDATE inventory SET reserved = 0")
        with self.db.connect("admin") as c, self.assertRaises(psycopg.Error):
            c.execute("UPDATE audit_events SET actor = 'x'")


if __name__ == "__main__":
    unittest.main()
