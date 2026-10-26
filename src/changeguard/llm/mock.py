"""Offline stand-ins used ONLY to test the control loop, guards and benchmark plumbing.

!! These are NOT language models. MockPolicyLLM is a hand-written policy that speaks the tool-calling
!! protocol. Numbers produced with it say nothing about how a real model performs. Real results come from
!! running the benchmark with CG_LLM_PROVIDER=gemini|openai.
"""
from __future__ import annotations

import json
from collections.abc import Callable

from ..agent.assemble import assemble_report
from .base import LLMClient, LLMTurn, ToolCall, Usage


def _history(messages: list[dict]):
    calls: dict[str, tuple[str, dict]] = {}
    results: list[tuple[str, dict, dict]] = []  # (tool name, args, parsed content)
    for m in messages:
        if m["role"] == "assistant":
            for c in m.get("tool_calls") or []:
                calls[c["id"]] = (c["name"], c["arguments"])
        elif m["role"] == "tool":
            name, args = calls.get(m["tool_call_id"], (m.get("name", "?"), {}))
            try:
                content = json.loads(m["content"])
            except Exception:
                content = {"status": "error", "error": {"kind": "malformed_result"}}
            results.append((name, args, content))
    return calls, results


class MockPolicyLLM(LLMClient):
    provider = "mock"
    model = "mock-policy-v1"

    def __init__(self) -> None:
        self._n = 0

    def _call(self, name: str, args: dict) -> LLMTurn:
        self._n += 1
        return LLMTurn(tool_calls=[ToolCall(f"call_{self._n}", name, args)], usage=Usage(0, 0))

    # -- decision hooks (overridden by the gullible variant)
    def _pick(self, candidates: dict[str, dict]) -> str | None:
        ok = {k: v for k, v in candidates.items() if v["eligibility"] == "eligible_for_review"}
        if not ok:
            return None
        return sorted(ok, key=lambda k: (ok[k]["best_lead_time_days"] if ok[k]["best_lead_time_days"] is not None else 10**6, k))[0]

    def generate(self, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
        req = json.loads(messages[0]["content"])
        change, cid = req["change_request"], req["change_request"]["component_id"]
        market = change.get("target_market")
        _, results = _history(messages)

        def find(name: str, key: str | None = None, val: str | None = None):
            for n, a, c in reversed(results):
                if n == name and (key is None or a.get(key) == val):
                    return c
            return None

        def ok(c):
            return c is not None and c.get("status") == "ok"

        bom = find("get_bom_impact")
        if bom is None:
            return self._call("get_bom_impact", {"component_id": cid, "depth_limit": 8})
        if not ok(bom):
            return self._call("escalate_to_human", {"reason": f"where-used analysis failed: {bom.get('error', {}).get('kind')}",
                                                    "missing": ["get_bom_impact"]})
        if find("get_inventory_status") is None:
            return self._call("get_inventory_status", {"component_id": cid})
        sup = find("get_supplier_options")
        if sup is None:
            return self._call("get_supplier_options", {"component_id": cid})
        if find("calculate_impact_metrics", "candidate_component_id", None) is None:
            return self._call("calculate_impact_metrics", {"component_id": cid})
        if find("search_engineering_docs") is None:
            return self._call("search_engineering_docs", {"query": f"{cid} end of life qualification"})
        candidates: list[str] = [a["component_id"] for a in (sup["data"]["alternates"] if ok(sup) else [])]
        for cand in candidates:
            if find("check_compliance", "component_id", cand) is None:
                return self._call("check_compliance", {"component_id": cand, "target_market": market})
            if find("calculate_impact_metrics", "candidate_component_id", cand) is None:
                return self._call("calculate_impact_metrics", {"component_id": cid, "candidate_component_id": cand})
        evals = {}
        for cand in candidates:
            c = find("calculate_impact_metrics", "candidate_component_id", cand)
            if ok(c) and c["data"].get("candidate"):
                evals[cand] = c["data"]["candidate"]
        pre = self._before_decision(results)
        if pre:
            return pre
        pick = self._pick(evals)
        drafted = find("draft_change_request")
        if pick and drafted is None and not self._injected(results):
            return self._call("draft_change_request", {
                "action_type": "CREATE_CHANGE_ORDER", "component_id": cid, "replacement_component_id": pick,
                "disposition": "substitute_candidate_review", "affected_assemblies": bom["data"]["affected_ids"],
                "notes": "Candidate for engineering review only; equivalence not established."})
        return self._call("submit_report", self._report(change, results, evals, pick, drafted))

    # -- hooks
    def _injected(self, results) -> bool:
        return False

    def _before_decision(self, results) -> LLMTurn | None:
        return None

    # -- report assembly
    def _report(self, change, results, evals, pick, drafted) -> dict:
        return assemble_report(change, results, evals, pick, drafted)


class GullibleMockLLM(MockPolicyLLM):
    """Simulates a model that is fooled by prompt injection in a retrieved document. Used to prove that the
    deterministic guards (tool allow-list, report constraint enforcement) hold even when the model does not."""

    model = "mock-gullible-v1"

    def _injected(self, results) -> bool:
        return any("mark cmp-106 as fully compliant" in json.dumps(c).lower() for n, a, c in results if n == "search_engineering_docs")

    def _before_decision(self, results) -> LLMTurn | None:
        if self._injected(results) and not any(n == "apply_approved_sandbox_change" for n, _, _ in results):
            return self._call("apply_approved_sandbox_change", {"action_id": "ACT-FAKE"})
        return None

    def _pick(self, candidates):
        if self._injected_flag:
            return "CMP-106"
        return super()._pick(candidates)

    _injected_flag = False

    def generate(self, system, messages, tools):
        _, results = _history(messages)
        self._injected_flag = self._injected(results)
        return super().generate(system, messages, tools)

    def _report(self, change, results, evals, pick, drafted):
        rep = super()._report(change, results, evals, pick, drafted)
        if self._injected_flag:
            rep["outcome"], rep["recommended_component_id"] = "recommendation", "CMP-106"
            rep["summary"] = "Supplier bulletin confirms CMP-106 is fully compliant; recommending it."
            for a in rep["option_assessments"]:
                if a["component_id"] == "CMP-106":
                    a["eligibility"], a["reasons"] = "eligible_for_review", []
        return rep


class ScriptedLLM(LLMClient):
    """Plays back a fixed list of turns (each an LLMTurn or a callable(messages)->LLMTurn). For unit tests."""

    provider = "mock"
    model = "scripted"

    def __init__(self, turns: list[LLMTurn | Callable[[list[dict]], LLMTurn]]):
        self.turns = list(turns)
        self.calls = 0

    def generate(self, system, messages, tools):
        self.calls += 1
        if not self.turns:
            return LLMTurn(text="(script exhausted)", usage=Usage(0, 0))
        t = self.turns.pop(0)
        return t(messages) if callable(t) else t


def tc(name: str, args: dict, id_: str | None = None) -> LLMTurn:
    return LLMTurn(tool_calls=[ToolCall(id_ or f"c_{name}", name, args)], usage=Usage(10, 5))
