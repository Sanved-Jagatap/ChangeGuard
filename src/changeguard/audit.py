"""Append-only audit trail."""
from __future__ import annotations

import json

from .db import Conn, utcnow


def audit(conn: Conn, actor: str, event_type: str, *, change_id: str | None = None, run_id: str | None = None,
          action_id: str | None = None, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events(ts, actor, event_type, change_id, run_id, action_id, detail_json) VALUES (?,?,?,?,?,?,?)",
        (utcnow(), actor, event_type, change_id, run_id, action_id, json.dumps(detail or {}, sort_keys=True, default=str)))
