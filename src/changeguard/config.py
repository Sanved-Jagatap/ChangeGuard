"""Settings from environment variables. Secrets never have defaults."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    database_url: str = "sqlite:///./data/changeguard.db"
    database_url_reader: str | None = None
    database_url_recorder: str | None = None
    database_url_sandbox: str | None = None
    llm_provider: str = "mock"
    llm_model: str = ""
    max_steps: int = 24
    max_retries_per_tool: int = 2
    wall_clock_seconds: float = 90.0
    max_total_tokens: int = 80_000
    tool_timeout_seconds: float = 10.0
    retry_backoff_seconds: float = 0.0
    max_agent_runs_per_day: int = 40
    demo_tokens: dict[str, tuple[str, str]] = field(default_factory=dict)  # token -> (user, role)
    anonymous_role: str = "viewer"
    allow_reset: bool = False
    test_hooks: bool = False
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    as_of: str = "2026-10-22"  # fixed evaluation date so seeded staleness is deterministic

    @classmethod
    def from_env(cls) -> Settings:
        tokens: dict[str, tuple[str, str]] = {}
        for item in filter(None, (os.environ.get("CG_DEMO_TOKENS") or "").split(",")):
            parts = item.strip().split(":")
            if len(parts) == 3:
                user, role, token = parts
                tokens[token] = (user, role)
        pricing: dict = {}
        raw = os.environ.get("CG_PRICING_JSON")
        if raw:
            pricing = json.loads(raw)
        return cls(
            database_url=os.environ.get("CG_DATABASE_URL", "sqlite:///./data/changeguard.db"),
            database_url_reader=os.environ.get("CG_DATABASE_URL_READER") or None,
            database_url_recorder=os.environ.get("CG_DATABASE_URL_RECORDER") or None,
            database_url_sandbox=os.environ.get("CG_DATABASE_URL_SANDBOX") or None,
            llm_provider=os.environ.get("CG_LLM_PROVIDER", "mock"),
            llm_model=os.environ.get("CG_LLM_MODEL", ""),
            max_steps=_int("CG_MAX_STEPS", 24),
            max_retries_per_tool=_int("CG_MAX_RETRIES_PER_TOOL", 2),
            wall_clock_seconds=_float("CG_WALL_CLOCK_SECONDS", 90.0),
            max_total_tokens=_int("CG_MAX_TOTAL_TOKENS", 80_000),
            tool_timeout_seconds=_float("CG_TOOL_TIMEOUT_SECONDS", 10.0),
            retry_backoff_seconds=_float("CG_RETRY_BACKOFF_SECONDS", 0.5),
            max_agent_runs_per_day=_int("CG_MAX_AGENT_RUNS_PER_DAY", 40),
            demo_tokens=tokens,
            anonymous_role=os.environ.get("CG_ANONYMOUS_ROLE", "viewer"),
            allow_reset=os.environ.get("CG_ALLOW_RESET", "0") == "1",
            test_hooks=os.environ.get("CG_TEST_HOOKS", "0") == "1",
            pricing=pricing,
        )
