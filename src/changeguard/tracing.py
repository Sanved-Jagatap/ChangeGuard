"""Per-run trace recording (steps, tool inputs/outputs, errors, latency, tokens).

The database is the source of truth and powers the UI timeline. An optional OpenTelemetry bridge
emits the same steps as spans (viewable in Phoenix, Jaeger, etc.) when OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .db import Conn, utcnow

MAX_STORED_CHARS = 60_000


def _dump(obj: Any) -> str | None:
    if obj is None:
        return None
    s = json.dumps(obj, sort_keys=True, default=str)
    return s if len(s) <= MAX_STORED_CHARS else s[:MAX_STORED_CHARS] + "...[truncated]"


class _OtelBridge:  # pragma: no cover - optional, needs opentelemetry packages
    def __init__(self) -> None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "changeguard"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])))
        self.tracer = provider.get_tracer("changeguard")

    def emit(self, run_id: str, step: dict) -> None:
        with self.tracer.start_as_current_span(f"{step['kind']}:{step['name']}") as span:
            span.set_attribute("changeguard.run_id", run_id)
            for k in ("kind", "name", "status", "attempt", "duration_ms", "input_tokens", "output_tokens", "error_kind"):
                if step.get(k) is not None:
                    span.set_attribute(f"changeguard.{k}", step[k])


def _make_otel():
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    try:
        return _OtelBridge()
    except Exception:  # missing packages or bad endpoint must never break a run
        return None


class TraceRecorder:
    def __init__(self, conn: Conn | None, run_id: str):
        self.conn = conn  # recorder-role connection (None => in-memory only)
        self.run_id = run_id
        self.steps: list[dict] = []
        self._otel = _make_otel()

    def step(self, kind: str, name: str, *, input: Any = None, output: Any = None, status: str = "ok",  # noqa: A002
             error_kind: str | None = None, error_message: str | None = None, attempt: int = 1,
             duration_ms: int = 0, input_tokens: int | None = None, output_tokens: int | None = None) -> int:
        idx = len(self.steps) + 1
        rec = {"idx": idx, "kind": kind, "name": name, "attempt": attempt, "input": input, "output": output,
               "status": status, "error_kind": error_kind, "error_message": error_message,
               "duration_ms": duration_ms, "input_tokens": input_tokens, "output_tokens": output_tokens}
        self.steps.append(rec)
        if self.conn is not None:
            self.conn.execute(
                "INSERT INTO agent_steps(run_id, idx, kind, name, attempt, input_json, output_json, status, error_kind, "
                "error_message, started_at, duration_ms, input_tokens, output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, idx, kind, name, attempt, _dump(input), _dump(output), status, error_kind,
                 (error_message or None) and error_message[:2000], utcnow(), duration_ms, input_tokens, output_tokens))
        if self._otel:
            try:
                self._otel.emit(self.run_id, rec)
            except Exception:
                pass
        return idx

    def ref(self, idx: int) -> str:
        return f"S{idx}"
