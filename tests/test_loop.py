import unittest

from changeguard.agent.loop import AgentConfig, AgentRunner
from changeguard.errors import LLMError
from changeguard.llm.base import LLMTurn, ToolCall, Usage
from changeguard.llm.mock import ScriptedLLM, tc
from changeguard.tools import FaultPlan, ToolContext, build_registry
from changeguard.tracing import TraceRecorder
from tests.helpers import make_service, submit

BASE_FIGS = {"affected_count": 7, "exposure_units": 3600, "available_units": 1400, "shortage_units": 2200}


def report(**over):
    r = {"outcome": "insufficient_evidence", "summary": "Not enough evidence to recommend.", "affected_ids": ["ASM-MCB1"],
         "key_figures": BASE_FIGS, "recommended_component_id": None}
    r.update(over)
    return r


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.change = self.svc.get_change(submit(self.svc)["change_id"])
        self.reader = self.svc.db.connect("reader")

    def run_agent(self, turns, faults=None, cfg=None, llm=None):
        reg = build_registry(self.svc.settings, faults)
        ctx = ToolContext(self.reader, self.svc.settings, self.change, run_id="RUN-T", approvals=self.svc.approvals)
        trace = TraceRecorder(None, "RUN-T")
        runner = AgentRunner(reg, llm or ScriptedLLM(turns), self.svc.settings, trace, cfg or AgentConfig())
        return runner.run(self.change, ctx)

    def test_blocks_write_tool_and_counts_it(self):
        res = self.run_agent([tc("apply_approved_sandbox_change", {"action_id": "ACT-1"}), tc("submit_report", report())])
        self.assertEqual(res.unsafe_requests, 1)
        self.assertTrue(any(s["kind"] == "guard" and s["name"] == "apply_approved_sandbox_change" and s["status"] == "blocked" for s in res.steps))
        self.assertEqual(res.status, "completed")

    def test_unknown_tool_is_blocked(self):
        res = self.run_agent([tc("rm_rf", {}), tc("submit_report", report())])
        self.assertEqual(res.unsafe_requests, 1)

    def test_max_steps_stops_run(self):
        res = self.run_agent([tc("get_bom_impact", {"component_id": "CMP-104"}, f"c{i}") for i in range(10)], cfg=AgentConfig(max_steps=3))
        self.assertEqual((res.status, res.stop_reason, res.outcome), ("stopped_limit", "max_steps", "escalated"))
        self.assertEqual(res.llm_turns, 3)

    def test_token_budget(self):
        big = [LLMTurn(tool_calls=[ToolCall(f"c{i}", "get_bom_impact", {"component_id": "CMP-104"})], usage=Usage(5000, 100)) for i in range(5)]
        res = self.run_agent(big, cfg=AgentConfig(max_total_tokens=6000))
        self.assertEqual(res.stop_reason, "token_budget")

    def test_wall_clock(self):
        res = self.run_agent([tc("get_bom_impact", {"component_id": "CMP-104"})], cfg=AgentConfig(wall_clock_seconds=-1))
        self.assertEqual(res.stop_reason, "wall_clock")

    def test_no_tool_call_protocol(self):
        res = self.run_agent([LLMTurn(text="hello", usage=Usage(1, 1))] * 5)
        self.assertEqual((res.status, res.stop_reason), ("failed", "no_tool_call"))

    def test_llm_error_fails_cleanly(self):
        def boom(_m):
            raise LLMError("rate limited")
        res = self.run_agent([boom])
        self.assertEqual((res.status, res.stop_reason, res.outcome), ("failed", "llm_error", "escalated"))

    def test_invalid_report_gets_one_retry_then_fails(self):
        res = self.run_agent([tc("submit_report", {"outcome": "recommendation"}), tc("submit_report", {"nope": 1})])
        self.assertEqual((res.status, res.stop_reason), ("failed", "invalid_report"))
        res = self.run_agent([tc("submit_report", {"outcome": "bad"}), tc("submit_report", report())])
        self.assertEqual(res.status, "completed")

    def test_long_claim_subject_is_rejected_then_corrected(self):
        long_subject = "This claim subject is deliberately longer than forty characters"
        bad = report(claims=[
            {"type": "metric", "subject": long_subject, "value": 2200, "evidence": ["S1"]}
        ])
        res = self.run_agent([
            tc("get_bom_impact", {"component_id": "CMP-104"}),
            tc("submit_report", bad),
            tc("submit_report", report(claims=[
                {"type": "metric", "subject": "CMP-104", "value": 2200, "evidence": ["S1"]}
            ]))
        ])
        self.assertEqual(res.status, "completed")
        rejected = [
            step for step in res.steps
            if step["kind"] == "guard" and step["name"] == "submit_report"
        ]
        self.assertTrue(rejected)
        self.assertEqual(rejected[0]["status"], "blocked")
        self.assertTrue(any("subject" in str(item) and "40" in str(item)
                            for item in rejected[0]["output"]["rejected"]))

    def test_recommending_unevaluated_candidate_is_rejected_then_overridden(self):
        bad = report(outcome="recommendation", recommended_component_id="CMP-109")
        res = self.run_agent([tc("submit_report", bad), tc("submit_report", bad)])
        self.assertEqual((res.status, res.stop_reason, res.guard_overrides), ("escalated", "guard_override", 1))
        self.assertIsNone(res.report["recommended_component_id"])

    def test_recommending_blocked_candidate_is_overridden(self):
        pre = [tc("calculate_impact_metrics", {"component_id": "CMP-104", "candidate_component_id": "CMP-106"})]
        bad = report(outcome="recommendation", recommended_component_id="CMP-106")
        res = self.run_agent(pre + [tc("submit_report", bad), tc("submit_report", bad)])
        self.assertEqual(res.stop_reason, "guard_override")
        self.assertIn("blocked", res.report["summary"])

    def test_model_can_correct_after_rejection(self):
        pre = [tc("calculate_impact_metrics", {"component_id": "CMP-104", "candidate_component_id": "CMP-106"})]
        bad = report(outcome="recommendation", recommended_component_id="CMP-106")
        res = self.run_agent(pre + [tc("submit_report", bad), tc("submit_report", report())])
        self.assertEqual((res.status, res.guard_overrides), ("completed", 0))

    def test_good_recommendation_accepted_with_verified_claims(self):
        turns = [tc("calculate_impact_metrics", {"component_id": "CMP-104", "candidate_component_id": "CMP-109"}),
                 tc("calculate_impact_metrics", {"component_id": "CMP-104"})]
        good = report(outcome="recommendation", recommended_component_id="CMP-109", claims=[
            {"type": "compliance", "subject": "CMP-109", "value": "eligible_for_review", "evidence": ["S2"]},
            {"type": "metric", "subject": "CMP-104", "value": 2200, "evidence": ["S4"]},
            {"type": "metric", "subject": "CMP-104", "value": 9999, "evidence": ["S4"]},  # fabricated number
            {"type": "other", "subject": "CMP-104", "value": "x", "evidence": ["S77"]}])  # nonexistent step
        res = self.run_agent(turns + [tc("submit_report", good)])
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.claim_check["total"], 4)
        self.assertEqual(res.claim_check["supported"], 2)
        self.assertAlmostEqual(res.claim_check["support_rate"], 0.5)

    def test_retry_is_transparent_and_traced(self):
        faults = FaultPlan({"get_bom_impact": ["timeout"]})
        res = self.run_agent([tc("get_bom_impact", {"component_id": "CMP-104"}), tc("submit_report", report())], faults=faults)
        bom_steps = [s for s in res.steps if s["name"] == "get_bom_impact"]
        self.assertEqual([s["status"] for s in bom_steps], ["retry", "ok"])
        self.assertEqual(res.retries, 1)

    def test_escalate_tool(self):
        res = self.run_agent([tc("escalate_to_human", {"reason": "evidence missing", "missing": ["lead time"]})])
        self.assertEqual((res.status, res.outcome), ("escalated", "escalated"))

    def test_steps_have_timing_and_tokens(self):
        res = self.run_agent([tc("get_bom_impact", {"component_id": "CMP-104"}), tc("submit_report", report())])
        llm_steps = [s for s in res.steps if s["kind"] == "llm"]
        self.assertTrue(all(s["input_tokens"] is not None for s in llm_steps))
        self.assertEqual(res.input_tokens, 20)


if __name__ == "__main__":
    unittest.main()
