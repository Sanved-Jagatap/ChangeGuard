"""Demo authentication/authorisation: static tokens mapped to roles. Replace with real SSO in production."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import PermissionDenied

ROLE_PERMS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "analyst": frozenset({"read", "submit", "investigate"}),
    "reviewer": frozenset({"read", "submit", "investigate", "decide", "execute"}),
    "admin": frozenset({"read", "submit", "investigate", "decide", "execute", "reset"}),
}


@dataclass(frozen=True)
class Principal:
    user: str
    role: str

    def can(self, perm: str) -> bool:
        return perm in ROLE_PERMS.get(self.role, frozenset())

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise PermissionDenied(f"role '{self.role}' may not '{perm}'")


SYSTEM = Principal("system", "admin")


def authenticate(settings, token: str | None) -> Principal:  # noqa: ANN001
    if token and token in settings.demo_tokens:
        user, role = settings.demo_tokens[token]
        return Principal(user, role)
    if token:
        raise PermissionDenied("invalid token")
    role = settings.anonymous_role if settings.anonymous_role in ROLE_PERMS else "viewer"
    return Principal("anonymous", role)
