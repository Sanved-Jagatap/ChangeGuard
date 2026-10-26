"""Google Gemini REST (generateContent) with native function calling."""
from __future__ import annotations

import os

from ..errors import LLMError
from .base import LLMClient, LLMTurn, ToolCall, Usage
from .http import post_json

_KEEP = {"type", "description", "enum", "properties", "required", "items", "nullable"}


def to_gemini_schema(s):
    """Convert our JSON schema into the OpenAPI subset Gemini accepts."""
    if not isinstance(s, dict):
        return s
    out: dict = {}
    t = s.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if "null" in t:
            out["nullable"] = True
        out["type"] = non_null[0] if len(non_null) == 1 else "string"
        if len(non_null) > 1:
            out["description"] = (s.get("description", "") + " (give as string)").strip()
    elif t:
        out["type"] = t
    for k, v in s.items():
        if k not in _KEEP or k == "type":
            continue
        if k == "properties":
            out["properties"] = {pk: to_gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out["items"] = to_gemini_schema(v)
        elif k == "enum":
            vals = [x for x in v if x is not None]
            out["enum"] = vals
            if len(vals) != len(v):
                out["nullable"] = True
        elif k == "required":
            out["required"] = [r for r in v if r in s.get("properties", {})]
        elif k == "description" and "description" in out:
            continue
        else:
            out[k] = v
    return out


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")

    def build_request(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        contents: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                parts = m.get("raw")  # replay exact parts (keeps any thought signatures)
                if not parts:
                    parts = []
                    if m.get("content"):
                        parts.append({"text": m["content"]})
                    for c in m.get("tool_calls") or []:
                        parts.append({"functionCall": {"name": c["name"], "args": c["arguments"]}})
                contents.append({"role": "model", "parts": parts})
            elif m["role"] == "tool":
                import json
                part = {"functionResponse": {"name": m["name"], "response": {"result": json.loads(m["content"])}}}
                if contents and contents[-1]["role"] == "user" and contents[-1].get("_tool"):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part], "_tool": True})
        for c in contents:
            c.pop("_tool", None)
        decls = [{"name": t["name"], "description": t["description"], "parameters": to_gemini_schema(t["parameters"])}
                 for t in tools]
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                "tools": [{"functionDeclarations": decls}],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}}
        temp = os.environ.get("CG_LLM_TEMPERATURE")
        if temp:
            body["generationConfig"] = {"temperature": float(temp)}
        return body

    def parse_response(self, data: dict) -> LLMTurn:
        try:
            cand = data["candidates"][0]
            parts = cand["content"]["parts"]
        except (KeyError, IndexError) as e:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (data.get("candidates") or [{}])[0].get("finishReason")
            raise LLMError(f"no usable candidate (reason: {reason})") from e
        text, calls = [], []
        for i, p in enumerate(parts):
            if "text" in p and not p.get("thought"):
                text.append(p["text"])
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall(f"call_{i}", fc["name"], dict(fc.get("args") or {})))
        u = data.get("usageMetadata") or {}
        return LLMTurn(text="".join(text) or None, tool_calls=calls,
                       usage=Usage(u.get("promptTokenCount"), u.get("candidatesTokenCount")), raw=parts)

    def generate(self, system, messages, tools):
        data = post_json(f"{self.base_url}/models/{self.model}:generateContent",
                         {"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                         self.build_request(system, messages, tools))
        return self.parse_response(data)
