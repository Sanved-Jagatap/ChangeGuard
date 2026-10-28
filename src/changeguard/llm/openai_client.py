"""OpenAI-compatible Chat Completions with native tool calling (works with any compatible endpoint)."""
from __future__ import annotations

import json
import os

from ..errors import LLMError
from .base import LLMClient, LLMTurn, ToolCall, Usage
from .http import post_json


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not set")

    def build_request(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        msgs = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                entry: dict = {"role": "assistant", "content": m.get("content")}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [{"id": c["id"], "type": "function",
                                            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                           for c in m["tool_calls"]]
                msgs.append(entry)
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        body: dict[str, object] = {"model": self.model, "messages": msgs, "tool_choice": "required",
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["parameters"]}} for t in tools]}
        temp = os.environ.get("CG_LLM_TEMPERATURE")
        if temp:
            body["temperature"] = float(temp)
        return body

    def parse_response(self, data: dict) -> LLMTurn:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError("unexpected response shape") from e
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"__unparseable__": c["function"]["arguments"]}
            calls.append(ToolCall(c["id"], c["function"]["name"], args))
        u = data.get("usage") or {}
        return LLMTurn(text=msg.get("content"), tool_calls=calls,
                       usage=Usage(u.get("prompt_tokens"), u.get("completion_tokens")), raw=msg)

    def generate(self, system, messages, tools):
        data = post_json(f"{self.base_url}/chat/completions",
                         {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                         self.build_request(system, messages, tools))
        return self.parse_response(data)
