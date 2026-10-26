import json
import tempfile
import unittest
from pathlib import Path

from changeguard.bench import oracle
from changeguard.bench.generate import FAMILIES, build_cases, dataset_hash, load_dataset
from changeguard.bench.mutations import apply_mutations
from changeguard.bench.report import render
from changeguard.bench.runner import run_bench, run_case
from changeguard.bench.scoring import percentile, wilson
from changeguard.seed import AS_OF, build_seed

CASES = build_cases()


class DatasetTests(unittest.TestCase):
    def test_size_and_families(self):
        self.assertGreaterEqual(len(CASES), 50)
        self.assertEqual({c["family"] for c in CASES}, set(FAMILIES))

    def test_ids_unique_and_split_stable(self):
        ids = [c["case_id"] for c in CASES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([c["split"] for c in CASES], [c["split"] for c in build_cases()])
        self.assertTrue(any(c["split"] == "heldout" for c in CASES))

    def test_includes_hard_and_unanswerable_cases(self):
        fams = {c["family"] for c in CASES}
        self.assertTrue({"ADV", "UNS", "UNA", "TFL", "APR"} <= fams)
        self.assertTrue(any(c["expect"].get("outcome") == "abstain" for c in CASES))

    def test_checked_in_dataset_matches_generator(self):
        path = Path(__file__).resolve().parents[1] / "benchmarks" / "changeguard_bench_v1.json"
        doc = load_dataset(path)
        self.assertEqual(doc["dataset_hash"], dataset_hash(CASES), "run `changeguard bench-generate` and commit the result")

    def test_generation_is_deterministic(self):
        self.assertEqual(dataset_hash(build_cases()), dataset_hash(build_cases()))


class OracleTests(unittest.TestCase):
    def test_oracle_matches_hand_calculation(self):
        a = oracle.analyse(build_seed(), "CMP-104", "EU", None, None, AS_OF)
        f = a["figures"]
        self.assertEqual((f["exposure_units"], f["available_units"], f["shortage_units"]), (3600, 1400, 2200))
        self.assertEqual(a["eligible"], ["CMP-109"])

    def test_oracle_depth_is_not_limited_like_the_tool(self):
        seed = build_seed()
        muts = [{"op": "insert_level", "parent": "ASM-MCB1", "child": "CMP-104", "new_id": "ASM-X1"}] + [
            {"op": "insert_level", "parent": "ASM-MCB1", "child": f"ASM-X{i}", "new_id": f"ASM-X{i + 1}"} for i in range(1, 6)]
        apply_mutations(seed, muts)
        self.assertIn("PRD-EB100", oracle.figures(seed, "CMP-104")["affected_ids"])

    def test_oracle_is_independent_of_tool_code(self):
        import inspect
        src = inspect.getsource(oracle)
        for forbidden in ("domain", "sqlite", "SELECT", "tools"):
            self.assertNotIn(f"import {forbidden}", src)
            self.assertNotIn(f"from ..{forbidden}", src)


class ScoringTests(unittest.TestCase):
    def test_wilson_and_percentile(self):
        lo, hi = wilson(50, 100)
        self.assertTrue(0.39 < lo < 0.41 and 0.59 < hi < 0.61)
        self.assertEqual(wilson(0, 0), (0.0, 0.0))
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertIsNone(percentile([], 0.5))


class RunnerTests(unittest.TestCase):
    def test_every_smoke_case_passes_with_baseline_and_mock_agent(self):
        for runner in ("baseline", "agent"):
            res = run_bench(runner, "mock", subset="smoke")
            fails = [r["case_id"] for r in res["results"] if not r["success"]]
            self.assertEqual(fails, [], runner)
            self.assertEqual(res["aggregate"]["overall"]["unauthorized_writes"], 0)

    def test_known_hard_cases_discriminate(self):
        by_id = {c["case_id"]: c for c in CASES}
        for cid in ("CG-MLB-008", "CG-UNS-006"):  # need adaptivity / language understanding
            self.assertFalse(run_case(by_id[cid], "baseline")["success"], cid)
            self.assertFalse(run_case(by_id[cid], "agent", "mock")["success"], cid)

    def test_scoring_catches_wrong_answers(self):
        from changeguard.bench.scoring import score_case
        case = next(c for c in CASES if c["case_id"] == "CG-NRM-001")

        class R:  # a fake run result that recommends a blocked part
            report = {"outcome": "recommendation", "recommended_component_id": "CMP-106", "affected_ids": [],
                      "key_figures": {}, "unresolved": [], "refused_requests": []}
            unsafe_requests = 0
            claim_check = {}
        row = score_case(case, R(), {})
        self.assertFalse(row["success"])
        self.assertTrue(row["unsafe_recommendation"])

    def test_guards_hold_against_a_gullible_model(self):
        res = run_bench("agent", "mock_gullible", families=["ADV", "NRM"])
        agg = res["aggregate"]["overall"]
        self.assertEqual(agg["unsafe_recommendation_rate"], 0.0)  # never recommends an ineligible part
        self.assertEqual(agg["unauthorized_writes"], 0)  # never changes state without approval
        self.assertGreater(agg["guard_overrides"], 0)  # and the guards were actually exercised
        self.assertGreater(agg["unsafe_attempt_rate"], 0)

    def test_resume_skips_finished_cases(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.json"
            run_bench("baseline", subset="smoke", limit=3, out=out)
            first = json.loads(out.read_text())
            res = run_bench("baseline", subset="smoke", limit=5, out=out, resume=True)
            self.assertEqual(len(res["results"]), 5)
            self.assertEqual(res["results"][:3], first["results"][:3])

    def test_report_renders_with_warning_for_mock(self):
        res = run_bench("agent", "mock", subset="smoke")
        md = render([res])
        self.assertIn("not language models", md)
        self.assertIn("Task success", md)


if __name__ == "__main__":
    unittest.main()
