"""Error taxonomy. Failure kinds are distinct so they can be handled and scored differently."""
from __future__ import annotations

RETRYABLE_KINDS = frozenset({"timeout", "temporary_db", "empty_response", "malformed_result"})


class ChangeGuardError(Exception):
    """Base class."""


class ValidationError(ChangeGuardError):
    """Invalid input (kind: invalid_input)."""

    kind = "invalid_input"

    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


class ToolError(ChangeGuardError):
    """A tool failed. `kind` is one of: invalid_input, not_found, timeout, temporary_db,
    empty_response, malformed_result, permission_denied, unsafe_proposal, tool_failure,
    retry_limit_exceeded."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    def to_dict(self) -> dict:
        return {"error": {"kind": self.kind, "message": self.message, "retryable": self.retryable}}


class UnsafeProposal(ToolError):
    def __init__(self, message: str):
        super().__init__("unsafe_proposal", message)


class PermissionDenied(ChangeGuardError):
    kind = "permission_denied"


class ApprovalError(ChangeGuardError):
    """Approval flow violation (kind: approval_invalid, hash_mismatch, already_decided, ...)."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


class LLMError(ChangeGuardError):
    """Provider/network/protocol failure when talking to a hosted model."""


class BudgetExceeded(ChangeGuardError):
    """Daily spending/run limit reached."""
