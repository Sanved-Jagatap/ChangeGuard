"""Run a tool through the registry and record every attempt in the trace."""
from __future__ import annotations

from ..errors import ToolError
from ..tracing import TraceRecorder
from .registry import ToolContext, ToolRegistry


def run_tool(registry: ToolRegistry, trace: TraceRecorder, name: str, args: dict, ctx: ToolContext) -> tuple[bool, dict, int, int]:
    """Returns (ok, data_or_error_dict, final_step_idx, attempts)."""
    last_idx = {"v": 0}

    def on_attempt(n: int, status: str, data, err: ToolError | None, ms: int) -> None:
        last_idx["v"] = trace.step("tool", name, input=args, output=data, status=status, attempt=n,
                                   error_kind=err.kind if err else None, error_message=err.message if err else None,
                                   duration_ms=ms)

    ok, payload, attempts = registry.call_with_retry(name, args, ctx, on_attempt)
    return ok, payload, last_idx["v"], attempts
