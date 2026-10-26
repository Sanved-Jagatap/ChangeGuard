import json
import unittest
from unittest import mock

from changeguard.config import Settings
from changeguard.errors import LLMError
from changeguard.llm.base import estimate_cost
from changeguard.llm.gemini import GeminiClient, to_gemini_schema
from changeguard.llm.http import post_json
from changeguard.llm.openai_client import OpenAIClient
from changeguard.tools import build_registry

HISTORY = [
    {"role": "user", "content": '{"change_request": {}}'},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "call_0", "name": "get_bom_impact", "arguments": {"component_id": "CMP-104"}}],
     "raw": None},
    {"role": "tool", "tool_call_id": "call_0", "name": "get_bom_impact", "content": json.dumps({"status": "ok", "data": {"x": 1}})},
]
TOOLS = [{"name": "get_bom_impact", "description": "d", "parameters": {"type": "object", "properties": {"component_id": {"type": "string"}}, "required": ["component_id"]}}]


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._p


class AdapterTests(unittest.TestCase):
    """These verify request/response translation against fixtures written from the providers' documented
    formats. They do NOT prove a live API call works: see docs/USER_TODO.md (live smoke test)."""

    def test_openai_request_and_parse(self):
        c = OpenAIClient("m", api_key="k")
        body = c.build_request("SYS", HISTORY, TOOLS)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(body["tool_choice"], "required")
        asst = body["messages"][2]
        self.assertEqual(asst["tool_calls"][0]["function"]["name"], "get_bom_impact")
        self.assertEqual(json.loads(asst["tool_calls"][0]["function"]["arguments"]), {"component_id": "CMP-104"})
        self.assertEqual(body["messages"][3]["role"], "tool")
        turn = c.parse_response({"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "submit_report", "arguments": json.dumps({"a": 1})}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}})
        self.assertEqual((turn.tool_calls[0].name, turn.tool_calls[0].arguments, turn.usage.input_tokens, turn.usage.output_tokens),
                         ("submit_report", {"a": 1}, 11, 7))

    def test_openai_bad_arguments_do_not_crash(self):
        turn = OpenAIClient("m", api_key="k").parse_response({"choices": [{"message": {"tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{not json"}}]}}]})
        self.assertIn("__unparseable__", turn.tool_calls[0].arguments)

    def test_gemini_request_and_parse(self):
        c = GeminiClient("m", api_key="k")
        body = c.build_request("SYS", HISTORY, TOOLS)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "SYS")
        self.assertEqual(body["toolConfig"]["functionCallingConfig"]["mode"], "ANY")
        roles = [x["role"] for x in body["contents"]]
        self.assertEqual(roles, ["user", "model", "user"])
        self.assertIn("functionCall", body["contents"][1]["parts"][0])
        self.assertEqual(body["contents"][2]["parts"][0]["functionResponse"]["response"]["result"]["data"], {"x": 1})
        raw = [{"functionCall": {"name": "get_bom_impact", "args": {"component_id": "CMP-104"}}, "thoughtSignature": "sig"}]
        parts = c.build_request("S", [HISTORY[0], {**HISTORY[1], "raw": raw}], TOOLS)["contents"][1]["parts"]
        self.assertEqual(parts, raw)  # thought signatures are replayed verbatim
        turn = c.parse_response({"candidates": [{"content": {"parts": [
            {"text": "hi"}, {"functionCall": {"name": "submit_report", "args": {"a": 1}}}]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}})
        self.assertEqual((turn.tool_calls[0].name, turn.usage.input_tokens), ("submit_report", 5))

    def test_gemini_parallel_tool_results_are_grouped(self):
        hist = [HISTORY[0], {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "name": "t1", "arguments": {}}, {"id": "b", "name": "t2", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "a", "name": "t1", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "name": "t2", "content": "{}"}]
        contents = GeminiClient("m", api_key="k").build_request("S", hist, TOOLS)["contents"]
        self.assertEqual(len(contents[-1]["parts"]), 2)

    def test_gemini_blocked_response_raises(self):
        with self.assertRaises(LLMError):
            GeminiClient("m", api_key="k").parse_response({"promptFeedback": {"blockReason": "SAFETY"}})

    def test_gemini_schema_conversion_for_all_real_tools(self):
        reg = build_registry(Settings())
        for spec in reg.llm_specs():
            out = to_gemini_schema(spec["parameters"])
            blob = json.dumps(out)
            for banned in ("additionalProperties", "pattern", "maxLength", "minLength", '"null"'):
                self.assertNotIn(banned, blob, f"{spec['name']} still has {banned}")
            self.assertEqual(out["type"], "object")

    def test_missing_keys_fail_fast_without_leaking(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(LLMError):
                GeminiClient("m")
            with self.assertRaises(LLMError):
                OpenAIClient("m")

    def test_http_retries_on_rate_limit_then_succeeds(self):
        seq = [FakeResp(429, {"e": 1}), FakeResp(503, {"e": 1}), FakeResp(200, {"ok": True})]
        with mock.patch("requests.post", side_effect=seq), mock.patch("time.sleep"):
            self.assertEqual(post_json("http://x", {}, {}), {"ok": True})

    def test_http_gives_up_and_never_echoes_headers(self):
        with mock.patch("requests.post", return_value=FakeResp(500, {"e": "boom"})), mock.patch("time.sleep"):
            with self.assertRaises(LLMError) as cm:
                post_json("http://x", {"Authorization": "Bearer SECRET"}, {}, retries=2)
        self.assertNotIn("SECRET", str(cm.exception))

    def test_cost_is_none_without_a_price_table(self):
        self.assertIsNone(estimate_cost({}, "m", 100, 100))
        self.assertIsNone(estimate_cost({"m": {"input_per_mtok": 1, "output_per_mtok": 2}}, "m", None, 5))
        self.assertEqual(estimate_cost({"m": {"input_per_mtok": 1, "output_per_mtok": 2}}, "m", 1_000_000, 500_000), 2.0)


if __name__ == "__main__":
    unittest.main()
