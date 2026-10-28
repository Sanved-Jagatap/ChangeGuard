"""Human-approval workflow. Approvals bind to an exact payload hash; execution re-checks everything."""
from __future__ import annotations

import json
import uuid

from . import actions as A
from .audit import audit
from .auth import Principal
from .config import Settings
from .db import Database, utcnow
from .errors import ApprovalError, PermissionDenied, UnsafeProposal, ValidationError


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class ApprovalService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.force_verification_failure = False  # honoured only when settings.test_hooks is true

    # ------------------------------------------------------------------ proposals
    def propose(self, change_id: str, run_id: str | None, action_type: str, payload: dict, actor: str) -> dict:
        payload = A.normalise(action_type, payload)
        with self.db.connect("reader") as reader:
            change = reader.query_one("SELECT * FROM engineering_changes WHERE change_id = ?", (change_id,))
            if change is None:
                raise ValidationError(f"unknown change_id {change_id}")
            A.precheck(reader, change, action_type, payload, self.settings.as_of)
        h = A.payload_hash(action_type, payload)
        aid = new_id("ACT")
        with self.db.connect("recorder") as rec, rec.tx():
            dup = rec.query_one("SELECT action_id FROM proposed_actions WHERE change_id = ? AND payload_hash = ? "
                                "AND status IN ('proposed','approved','executed','verified')", (change_id, h))
            if dup:
                return self.get_action(dup["action_id"]) | {"duplicate_of_existing": True}
            rec.execute(
                "INSERT INTO proposed_actions(action_id, change_id, run_id, action_type, payload_json, payload_hash, "
                "status, supersedes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, change_id, run_id, action_type, json.dumps(payload, sort_keys=True), h, "proposed", None, utcnow()))
            audit(rec, actor, "action_proposed", change_id=change_id, run_id=run_id, action_id=aid,
                  detail={"action_type": action_type, "payload_hash": h})
        return self.get_action(aid)

    def get_action(self, action_id: str) -> dict:
        with self.db.connect("reader") as r:
            row = r.query_one("SELECT * FROM proposed_actions WHERE action_id = ?", (action_id,))
            if row is None:
                raise ApprovalError("not_found", f"unknown action {action_id}")
            row["payload"] = json.loads(row["payload_json"])
            row["approvals"] = r.query("SELECT * FROM approvals WHERE action_id = ? ORDER BY decided_at", (action_id,))
            row["execution"] = r.query_one("SELECT * FROM executed_actions WHERE action_id = ?", (action_id,))
            return row

    def list_actions(self, change_id: str) -> list[dict]:
        with self.db.connect("reader") as r:
            ids = [x["action_id"] for x in r.query(
                "SELECT action_id FROM proposed_actions WHERE change_id = ? ORDER BY created_at", (change_id,))]
        return [self.get_action(i) for i in ids]

    # ------------------------------------------------------------------ decisions
    def decide(self, principal: Principal, action_id: str, decision: str, presented_hash: str, reason: str | None = None) -> dict:
        action = self.get_action(action_id)
        try:
            principal.require("decide")
        except PermissionDenied:
            self._audit_denied(principal, action, "decide")
            raise
        if decision not in ("approved", "rejected", "revision_requested"):
            raise ValidationError("decision must be approved, rejected or revision_requested")
        if action["status"] != "proposed":
            raise ApprovalError("already_decided", f"action is '{action['status']}', not 'proposed'")
        recomputed = A.payload_hash(action["action_type"], action["payload"])
        if presented_hash != action["payload_hash"] or recomputed != action["payload_hash"]:
            with self.db.connect("recorder") as rec:
                audit(rec, principal.user, "approval_hash_mismatch", change_id=action["change_id"], action_id=action_id,
                      detail={"presented": presented_hash, "stored": action["payload_hash"]})
            raise ApprovalError("hash_mismatch", "the payload you reviewed is not the payload on record")
        status = {"approved": "approved", "rejected": "rejected", "revision_requested": "revision_requested"}[decision]
        with self.db.connect("recorder") as rec, rec.tx():
            rec.execute(
                "INSERT INTO approvals(approval_id, action_id, payload_hash, decision, reviewer, role, reason, decided_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (new_id("APR"), action_id, action["payload_hash"], decision, principal.user, principal.role, reason, utcnow()))
            rec.execute("UPDATE proposed_actions SET status = ? WHERE action_id = ? AND status = 'proposed'", (status, action_id))
            audit(rec, principal.user, f"approval_{decision}", change_id=action["change_id"], action_id=action_id,
                  detail={"payload_hash": action["payload_hash"], "reason": reason})
        return self.get_action(action_id)

    def revise(self, principal: Principal, action_id: str, new_payload: dict) -> dict:
        """A reviewer edits the payload. This creates a NEW proposal; old approvals can never apply to it."""
        principal.require("decide")
        old = self.get_action(action_id)
        if old["status"] not in ("proposed", "revision_requested"):
            raise ApprovalError("not_revisable", f"action is '{old['status']}'")
        new = self.propose(old["change_id"], old["run_id"], old["action_type"], new_payload, principal.user)
        with self.db.connect("recorder") as rec, rec.tx():
            rec.execute("UPDATE proposed_actions SET status='superseded' WHERE action_id = ?", (action_id,))
            rec.execute("UPDATE proposed_actions SET supersedes = ? WHERE action_id = ?", (action_id, new["action_id"]))
            audit(rec, principal.user, "action_revised", change_id=old["change_id"], action_id=new["action_id"],
                  detail={"supersedes": action_id})
        return self.get_action(new["action_id"])

    # ------------------------------------------------------------------ execution
    def execute(self, principal: Principal, action_id: str) -> dict:
        action = self.get_action(action_id)
        try:
            principal.require("execute")
        except PermissionDenied:
            self._audit_denied(principal, action, "execute")
            raise
        if action["execution"] is not None:  # idempotent replay: never write twice
            return action | {"idempotent_replay": True}
        if action["status"] != "approved":
            raise ApprovalError("not_approved", f"action is '{action['status']}'; only approved actions can execute")
        approval = next((a for a in action["approvals"] if a["decision"] == "approved"), None)
        recomputed = A.payload_hash(action["action_type"], action["payload"])
        if approval is None or approval["payload_hash"] != recomputed or action["payload_hash"] != recomputed:
            with self.db.connect("recorder") as rec:
                audit(rec, principal.user, "execution_blocked_payload_changed", change_id=action["change_id"], action_id=action_id)
            raise ApprovalError("payload_changed", "approved payload differs from the stored payload; execution blocked")
        # re-check business rules against CURRENT state (approvals can go stale)
        with self.db.connect("reader") as reader:
            change = reader.query_one(
                "SELECT * FROM engineering_changes WHERE change_id = ?",
                (action["change_id"],),
            )
            if change is None:
                raise ApprovalError(
                    "stale_state",
                    "associated engineering change no longer exists; execution blocked",
                )
            try:
                A.precheck(reader, change, action["action_type"], action["payload"], self.settings.as_of)
            except UnsafeProposal as e:
                self._record_failure(principal, action, approval, f"stale_state: {e.message}")
                raise ApprovalError("stale_state", e.message) from e
        exec_id = new_id("EXE")
        try:
            with self.db.connect("sandbox_writer") as w, w.tx():
                before, after = A.apply_action(w, action, action["payload"])
                w.execute(
                    "INSERT INTO executed_actions(exec_id, action_id, approval_id, payload_hash, before_json, after_json, "
                    "outcome, error, executed_at, verification_status, verification_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (exec_id, action_id, approval["approval_id"], action["payload_hash"], json.dumps(before), json.dumps(after),
                     "applied", None, utcnow(), "pending", None))
                w.execute("UPDATE proposed_actions SET status='executed' WHERE action_id = ?", (action_id,))
                audit(w, principal.user, "action_executed", change_id=action["change_id"], action_id=action_id,
                      detail={"exec_id": exec_id, "before": before, "after": after, "payload_hash": action["payload_hash"]})
        except ApprovalError as e:
            self._record_failure(principal, action, approval, f"{e.kind}: {e.message}")
            raise
        expected_after = dict(after)
        if self.settings.test_hooks and self.force_verification_failure:
            expected_after = {k: (v + 1 if isinstance(v, int) else "tampered") for k, v in after.items()}
        with self.db.connect("reader") as reader:
            ok, detail = A.verify_action(reader, action, action["payload"], before, expected_after)
        with self.db.connect("sandbox_writer") as w, w.tx():
            w.execute("UPDATE executed_actions SET verification_status = ?, verification_detail = ? WHERE exec_id = ?",
                      ("passed" if ok else "failed", detail, exec_id))
            w.execute("UPDATE proposed_actions SET status = ? WHERE action_id = ?",
                      ("verified" if ok else "verification_failed", action_id))
            audit(w, principal.user, "verification_passed" if ok else "verification_failed",
                  change_id=action["change_id"], action_id=action_id, detail={"exec_id": exec_id, "detail": detail})
        return self.get_action(action_id)

    # ------------------------------------------------------------------ helpers
    def _record_failure(self, principal: Principal, action: dict, approval: dict, error: str) -> None:
        with self.db.connect("sandbox_writer") as w, w.tx():
            w.execute(
                "INSERT INTO executed_actions(exec_id, action_id, approval_id, payload_hash, before_json, after_json, "
                "outcome, error, executed_at, verification_status, verification_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("EXE"), action["action_id"], approval["approval_id"], action["payload_hash"], "{}", "{}",
                 "failed", error, utcnow(), "failed", "not applied"))
            w.execute("UPDATE proposed_actions SET status='failed' WHERE action_id = ?", (action["action_id"],))
            audit(w, principal.user, "execution_failed", change_id=action["change_id"], action_id=action["action_id"],
                  detail={"error": error})

    def _audit_denied(self, principal: Principal, action: dict, what: str) -> None:
        with self.db.connect("recorder") as rec:
            audit(rec, principal.user, "permission_denied", change_id=action["change_id"], action_id=action["action_id"],
                  detail={"attempted": what, "role": principal.role})
