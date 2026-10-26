"""Provider-neutral LLM interface. Internal message format (OpenAI-like):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str|None, "tool_calls": [{"id","name","arguments"}], "raw": any}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str(JSON)}
The system prompt is passed separately."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class LLMTurn:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    raw: Any = None


class LLMClient(ABC):
    provider: str = "unknown"
    model: str = "unknown"

    @abstractmethod
    def generate(self, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn: ...


def estimate_cost(pricing: dict, model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Cost in USD, or None when the price table has no entry (never guessed)."""
    p = pricing.get(model)
    if not p or input_tokens is None or output_tokens is None:
        return None
    return round(input_tokens / 1e6 * p["input_per_mtok"] + output_tokens / 1e6 * p["output_per_mtok"], 6)
