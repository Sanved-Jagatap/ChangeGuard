"""Allow-listed sandbox actions: validation, deterministic pre-conditions, apply, verify.

Only two action types exist. Nothing else can ever be executed, regardless of what a model asks for.
  CREATE_CHANGE_ORDER  inserts a DRAFT change order (never 'released')
  RESERVE_STOCK_HOLD   increases `reserved` for an at-risk component at one site
"""
from __future__ import annotations

import hashlib
import json

from .db import Conn, utcnow
from .domain.bom import get_component, where_used
from .domain.metrics import evaluate_candidate
from .errors import ApprovalError, UnsafeProposal, ValidationError
from .schemas import ID_PATTERN, SITE_PATTERN, require_valid

ACTION_SCHEMAS: dict[str, dict] = {
    "CREATE_CHANGE_ORDER": {
        "type": "object", "additionalProperties": False,
        "required": ["change_id", "component_id", "disposition"],
        "properties": {
            "change_id": {"type": "string", "maxLength": 40},
            "component_id": {"type": "string", "pattern": ID_PATTERN},
            "replacement_component_id": {"type": ["string", "null"], "pattern": ID_PATTERN},
            "disposition": {"type": "string", "enum": ["substitute_candidate_review", "last_time_buy", "redesign_review"]},
            "affected_assemblies": {"type": "array", "maxItems": 50, "items": {"type": "string", "pattern": ID_PATTERN}},
            "notes": {"type": ["string", "null"], "maxLength": 500},
        },
    },
    "RESERVE_STOCK_HOLD": {
        "type": "object", "additionalProperties": False,
        "required": ["change_id", "component_id", "site_id", "hold_qty"],
        "properties": {
            "change_id": {"type": "string", "maxLength": 40},
            "component_id": {"type": "string", "pattern": ID_PATTERN},
            "site_id": {"type": "string", "pattern": SITE_PATTERN},
            "hold_qty": {"type": "integer", "minimum": 1, "maximum": 100000},
            "reason": {"type": ["string", "null"], "maxLength": 200},
        },
    },
}
ACTION_TYPES = tuple(ACTION_SCHEMAS)


def normalise(action_type: str, payload: dict) -> dict:
    if action_type not in ACTION_SCHEMAS:
        raise ValidationError(f"action_type must be one of {list(ACTION_SCHEMAS)}")
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    require_valid(ACTION_SCHEMAS[action_type], payload)
    out = dict(payload)
    if action_type == "CREATE_CHANGE_ORDER":
        out["affected_assemblies"] = sorted(set(out.get("affected_assemblies") or []))
        out.setdefault("replacement_component_id", None)
        out.setdefault("notes", None)
    else:
        out.setdefault("reason", None)
    return out


def payload_hash(action_type: str, payload: dict) -> str:
    canon = json.dumps({"action_type": action_type, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def precheck(reader: Conn, change: dict, action_type: str, payload: dict, as_of: str) -> None:
    """Deterministic business rules. Raises UnsafeProposal. Run at proposal time AND again at execution time."""
    comp = get_component(reader, payload["component_id"])
    if payload["component_id"] != change["component_id"]:
        raise UnsafeProposal("actions may only target the component named in the change request")
    if payload["change_id"] != change["change_id"]:
        raise UnsafeProposal("payload change_id does not match the change request")
    constraints = json.loads(change["constraints_json"] or "{}")
    if action_type == "CREATE_CHANGE_ORDER":
        disp = payload["disposition"]
        repl = payload.get("replacement_component_id")
        if disp == "substitute_candidate_review":
            if not repl:
                raise UnsafeProposal("substitute_candidate_review requires replacement_component_id")
            if repl == payload["component_id"]:
                raise UnsafeProposal("replacement must differ from the component")
            ev = evaluate_candidate(reader, payload["component_id"], repl, change.get("target_market"),
                                    constraints.get("max_lead_time_days"), as_of)
            if ev["eligibility"] != "eligible_for_review":
                raise UnsafeProposal(
                    f"candidate {repl} is {ev['eligibility']} ({', '.join(ev['reasons']) or 'n/a'}); a change order "
                    "may only be drafted for an eligible_for_review candidate")
        else:
            if repl:
                raise UnsafeProposal(f"{disp} must not name a replacement component")
            if disp == "last_time_buy" and comp["lifecycle_status"] not in ("discontinued", "last_time_buy"):
                raise UnsafeProposal("last_time_buy requires a discontinued or last_time_buy component")
        known = set(where_used(reader, payload["component_id"])["affected_ids"])
        unknown = [a for a in payload.get("affected_assemblies", []) if a not in known]
        if unknown:
            raise UnsafeProposal(f"affected_assemblies not in the where-used result: {unknown}")
    else:
        if comp["lifecycle_status"] not in ("discontinued", "last_time_buy"):
            raise UnsafeProposal("stock holds are only allowed for discontinued or last_time_buy components")
        inv = reader.query_one("SELECT on_hand, reserved FROM inventory WHERE component_id = ? AND site_id = ?",
                               (payload["component_id"], payload["site_id"]))
        if inv is None:
            raise UnsafeProposal("no inventory record for that component at that site")
        if payload["hold_qty"] > int(inv["on_hand"]) - int(inv["reserved"]):
            raise UnsafeProposal("hold_qty exceeds available stock")


def apply_action(writer: Conn, action: dict, payload: dict) -> tuple[dict, dict]:
    """Run inside the sandbox writer's transaction. Returns (before, after)."""
    if action["action_type"] == "CREATE_CHANGE_ORDER":
        co_id = "CO-" + action["payload_hash"][:12].upper()
        writer.execute(
            "INSERT INTO change_orders(co_id, change_id, component_id, replacement_component_id, disposition, "
            "affected_assemblies_json, notes, status, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (co_id, payload["change_id"], payload["component_id"], payload.get("replacement_component_id"),
             payload["disposition"], json.dumps(payload.get("affected_assemblies", [])), payload.get("notes"),
             "draft", action["payload_hash"], utcnow()))
        return {}, {"co_id": co_id, "status": "draft"}
    row = writer.query_one("SELECT on_hand, reserved FROM inventory WHERE component_id = ? AND site_id = ?",
                           (payload["component_id"], payload["site_id"]))
    if row is None:
        raise ApprovalError("stale_state", "inventory row no longer exists")
    n = writer.execute(
        "UPDATE inventory SET reserved = reserved + ? WHERE component_id = ? AND site_id = ? AND (on_hand - reserved) >= ?",
        (payload["hold_qty"], payload["component_id"], payload["site_id"], payload["hold_qty"]))
    if n != 1:
        raise ApprovalError("stale_state", "available stock no longer covers the hold")
    return ({"reserved": int(row["reserved"])}, {"reserved": int(row["reserved"]) + payload["hold_qty"]})


def verify_action(reader: Conn, action: dict, payload: dict, before: dict, after: dict) -> tuple[bool, str]:
    """Re-read state and confirm the intended effect (and nothing else we can cheaply check)."""
    if action["action_type"] == "CREATE_CHANGE_ORDER":
        row = reader.query_one("SELECT * FROM change_orders WHERE co_id = ?", (after["co_id"],))
        if row is None:
            return False, "change order row not found"
        checks = {
            "status_draft": row["status"] == "draft",
            "payload_hash": row["payload_hash"] == action["payload_hash"],
            "component": row["component_id"] == payload["component_id"],
            "replacement": row["replacement_component_id"] == payload.get("replacement_component_id"),
            "disposition": row["disposition"] == payload["disposition"],
        }
    else:
        row = reader.query_one("SELECT reserved FROM inventory WHERE component_id = ? AND site_id = ?",
                               (payload["component_id"], payload["site_id"]))
        checks = {"reserved_matches": row is not None and int(row["reserved"]) == after["reserved"]}
    bad = [k for k, v in checks.items() if not v]
    return (not bad), ("all checks passed" if not bad else f"failed checks: {bad}")
