"""Tool registry: schema validation, permissions, timeouts, fault injection, result-shape checks."""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..db import Conn
from ..errors import ToolError, ValidationError
from ..schemas import require_valid


@dataclass
class ToolContext:
    reader: Conn
    settings: Settings
    change: dict  # engineering_changes row (the REQUEST decides hard constraints, not the model)
    run_id: str | None = None
    approvals: Any = None  # ApprovalService
    actor: str = "agent"
    proposals_made: int = 0
    max_proposals: int = 2


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable[[ToolContext, dict], dict]
    permission: str  # read | propose | control | write
    exposed: bool = True  # offered to the model by default?


@dataclass
class FaultPlan:
    """Injected failures for tests and the benchmark: tool name -> ordered list of per-attempt faults.
    Fault names: ok | timeout | temporary_db | empty | malformed | permanent"""

    faults: dict[str, list[str]] = field(default_factory=dict)

    def next(self, tool: str) -> str:
        q = self.faults.get(tool)
        return q.pop(0) if q else "ok"


class ToolRegistry:
    def __init__(self, settings: Settings, faults: FaultPlan | None = None):
        self.settings = settings
        self.faults = faults or FaultPlan()
        self.tools: dict[str, ToolSpec] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cg-tool")

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def llm_specs(self, include_write: bool = False) -> list[dict]:
        out = []
        for t in self.tools.values():
            if t.permission == "write" and not include_write:
                continue
            if not t.exposed and not include_write:
                continue
            out.append({"name": t.name, "description": t.description, "parameters": t.schema})
        return out

    def is_exposed(self, name: str) -> bool:
        t = self.tools.get(name)
        return bool(t and t.exposed and t.permission != "write")

    def call_once(self, name: str, args: dict, ctx: ToolContext) -> dict:
        """One attempt. Raises ToolError. Never returns a non-dict."""
        spec = self.tools.get(name)
        if spec is None:
            raise ToolError("permission_denied", f"unknown tool '{name}'")
        try:
            require_valid(spec.schema, args)
        except ValidationError as e:
            raise ToolError("invalid_input", "; ".join(e.errors)) from e
        fault = self.faults.next(name)
        if fault == "timeout":
            raise ToolError("timeout", "injected timeout")
        if fault == "temporary_db":
            raise ToolError("temporary_db", "injected temporary database error")
        if fault == "permanent":
            raise ToolError("tool_failure", "injected permanent failure")
        if fault == "empty":
            result: Any = {}
        elif fault == "malformed":
            result = "<<garbled>>"
        else:
            fut = self._pool.submit(spec.handler, ctx, args)
            try:
                result = fut.result(timeout=self.settings.tool_timeout_seconds)
            except FutTimeout as e:
                raise ToolError("timeout", f"tool exceeded {self.settings.tool_timeout_seconds}s") from e
            except ToolError:
                raise
            except ValidationError as e:
                raise ToolError("invalid_input", "; ".join(e.errors)) from e
        if not isinstance(result, dict):
            raise ToolError("malformed_result", "tool returned a non-object result")
        if not result:
            raise ToolError("empty_response", "tool returned an empty result")
        return result

    def call_with_retry(self, name: str, args: dict, ctx: ToolContext,
                        on_attempt: Callable[[int, str, Any, ToolError | None, int], None] | None = None) -> tuple[bool, dict, int]:
        """Retry retryable failures up to settings.max_retries_per_tool. Returns (ok, data_or_error_dict, attempts)."""
        attempts = 0
        max_attempts = 1 + max(0, self.settings.max_retries_per_tool)
        while True:
            attempts += 1
            t0 = time.perf_counter()
            try:
                data = self.call_once(name, args, ctx)
                ms = int((time.perf_counter() - t0) * 1000)
                if on_attempt:
                    on_attempt(attempts, "ok", data, None, ms)
                return True, data, attempts
            except ToolError as e:
                ms = int((time.perf_counter() - t0) * 1000)
                final = (not e.retryable) or attempts >= max_attempts
                if on_attempt:
                    on_attempt(attempts, "error" if final else "retry", None, e, ms)
                if final:
                    err = e if not e.retryable else ToolError("retry_limit_exceeded", f"{e.kind} persisted after {attempts} attempts: {e.message}")
                    return False, err.to_dict(), attempts
                if self.settings.retry_backoff_seconds:
                    time.sleep(self.settings.retry_backoff_seconds * attempts)
