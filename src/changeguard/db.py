"""Database access with enforced least-privilege roles.

Roles
  admin           migrations, seeding, reset (never used by the agent)
  reader          SELECT only. The only role the agent's read tools ever hold.
  recorder        workflow state: changes, runs, steps, proposals, approvals, audit
  sandbox_writer  the only role that can change sandbox business data (change orders,
                  inventory reservations) and it can do nothing else.

SQLite: reader opens the file read-only at the OS level; recorder/sandbox_writer get a
statement authorizer that allow-lists (table, operation) pairs and denies all DDL/PRAGMA.
PostgreSQL: separate login roles with GRANTs (see migrations/postgres_roles.sql).
SQL in this code base uses `?` placeholders and portable syntax; the Postgres adapter converts them.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROLES = ("admin", "reader", "recorder", "sandbox_writer")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_INSERT, _UPDATE, _DELETE = sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE

# (table -> allowed operations) per restricted SQLite role
_WRITE_ALLOW: dict[str, dict[str, set[int]]] = {
    "recorder": {
        "engineering_changes": {_INSERT, _UPDATE},
        "agent_runs": {_INSERT, _UPDATE},
        "agent_steps": {_INSERT},
        "proposed_actions": {_INSERT, _UPDATE},
        "approvals": {_INSERT},
        "audit_events": {_INSERT},
    },
    "sandbox_writer": {
        "change_orders": {_INSERT},
        "inventory": {_UPDATE},
        "executed_actions": {_INSERT, _UPDATE},
        "audit_events": {_INSERT},
        "proposed_actions": {_UPDATE},
    },
}
_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_SAVEPOINT,
}


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _authorizer(role: str):
    allow = _WRITE_ALLOW.get(role, {})

    def auth(action, arg1, arg2, dbname, source):  # noqa: ANN001
        if action in _READ_ACTIONS:
            return sqlite3.SQLITE_OK
        if action in (_INSERT, _UPDATE, _DELETE) and action in allow.get(arg1, set()):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return auth


class Conn:
    """Thin uniform wrapper over a sqlite3 or psycopg connection."""

    def __init__(self, raw: Any, dialect: str, role: str):
        self.raw = raw
        self.dialect = dialect
        self.role = role
        self._depth = 0

    # -- helpers
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        if self.dialect == "sqlite":
            cur = self.raw.execute(sql, tuple(params))
            cols = [c[0] for c in cur.description or []]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        cur = self.raw.execute(self._sql(sql), tuple(params))
        return list(cur.fetchall()) if cur.description else []

    def query_one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        cur = self.raw.execute(self._sql(sql), tuple(params))
        return cur.rowcount

    def executescript(self, sql: str) -> None:
        if self.dialect == "sqlite":
            self.raw.executescript(sql)
        else:
            self.raw.execute(sql)

    @contextmanager
    def tx(self) -> Iterator[Conn]:
        """Transaction. Nested use joins the outer transaction."""
        if self._depth > 0:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.raw.execute("BEGIN")
        self._depth = 1
        try:
            yield self
        except BaseException:
            self.raw.execute("ROLLBACK")
            raise
        else:
            self.raw.execute("COMMIT")
        finally:
            self._depth = 0

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> Conn:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Database:
    def __init__(
        self,
        url: str,
        reader_url: str | None = None,
        recorder_url: str | None = None,
        sandbox_url: str | None = None,
    ):
        self.url = url
        self.role_urls = {"reader": reader_url, "recorder": recorder_url, "sandbox_writer": sandbox_url}
        self.dialect = "sqlite" if url.startswith("sqlite") else "postgres"

    @classmethod
    def from_settings(cls, s) -> Database:  # noqa: ANN001
        return cls(s.database_url, s.database_url_reader, s.database_url_recorder, s.database_url_sandbox)

    # -- sqlite path
    @property
    def sqlite_path(self) -> str:
        assert self.dialect == "sqlite"
        raw = self.url.split("sqlite:///", 1)[1]
        return raw

    def connect(self, role: str = "reader") -> Conn:
        if role not in ROLES:
            raise ValueError(f"unknown role {role}")
        if self.dialect == "sqlite":
            return self._connect_sqlite(role)
        return self._connect_postgres(role)

    def _connect_sqlite(self, role: str) -> Conn:
        path = self.sqlite_path
        if role == "reader":
            raw = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                                  isolation_level=None, timeout=15, check_same_thread=False)
            raw.execute("PRAGMA query_only=ON")
        else:
            if role == "admin":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            raw = sqlite3.connect(path, isolation_level=None, timeout=15, check_same_thread=False)
        raw.execute("PRAGMA foreign_keys=ON")
        if role in _WRITE_ALLOW:
            raw.set_authorizer(_authorizer(role))
        elif role == "reader":
            raw.set_authorizer(_authorizer("reader"))
        return Conn(raw, "sqlite", role)

    def _connect_postgres(self, role: str) -> Conn:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Postgres support needs: pip install 'changeguard[postgres]'") from e
        url = self.role_urls.get(role) or self.url
        raw = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        return Conn(raw, "postgres", role)

    # -- migrations
    def migrate(self) -> list[str]:
        """Apply pending migrations as admin. Returns the versions applied."""
        applied: list[str] = []
        autoinc = (
            "INTEGER PRIMARY KEY AUTOINCREMENT"
            if self.dialect == "sqlite"
            else "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
        )
        suffix = "sqlite" if self.dialect == "sqlite" else "postgres"
        files = [MIGRATIONS_DIR / "001_schema.sql", MIGRATIONS_DIR / f"002_append_only_{suffix}.sql"]
        with self.connect("admin") as conn:
            have: set[str] = set()
            try:
                have = {r["version"] for r in conn.query("SELECT version FROM schema_migrations")}
            except Exception:
                have = set()
            for f in files:
                if f.stem in have:
                    continue
                sql = f.read_text(encoding="utf-8").replace("{{AUTOINC_PK}}", autoinc)
                conn.executescript(sql)
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                             (f.stem, utcnow()))
                applied.append(f.stem)
        return applied

    def apply_postgres_roles(self, passwords: dict[str, str]) -> None:
        """Create least-privilege roles (PostgreSQL only). Passwords must be alphanumeric."""
        if self.dialect != "postgres":
            return
        sql = (MIGRATIONS_DIR / "postgres_roles.sql").read_text(encoding="utf-8")
        for key, placeholder in (("reader", "{{READER_PW}}"), ("recorder", "{{RECORDER_PW}}"),
                                 ("sandbox", "{{SANDBOX_PW}}")):
            pw = passwords[key]
            if not pw.isalnum() and not all(c.isalnum() or c in "_-" for c in pw):
                raise ValueError("role passwords may contain only letters, digits, _ and -")
            sql = sql.replace(placeholder, pw)
        with self.connect("admin") as conn:
            conn.executescript(sql)

    def drop_all(self) -> None:
        """Destroy everything (sandbox reset only; callers must check the CG_ALLOW_RESET switch)."""
        if self.dialect == "sqlite":
            p = Path(self.sqlite_path)
            for suffix in ("", "-journal", "-wal", "-shm"):
                q = Path(str(p) + suffix)
                if q.exists():
                    q.unlink()
        else:  # pragma: no cover
            with self.connect("admin") as conn:
                conn.executescript("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
