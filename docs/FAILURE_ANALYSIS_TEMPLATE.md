# Failure analysis (fill after a real-model run)

Pick 5-10 failed cases from `benchmarks/published/<your run>.json` (`success: false`). For each:

| Case | What the model did (trace steps) | Root cause | Category | Fix / decision |
|---|---|---|---|---|
| CG-... | e.g. never raised depth_limit after `truncated=true` | no instruction to react to truncation | prompt / tool design / model limit | |

Categories: prompt gap, tool design, model limitation, benchmark label problem, guard too strict, guard too loose.
Also record: cases where the guard fired (`guard_overrides`), cases where retries rescued a run, and any case you decided the **benchmark** was wrong about (fix the oracle, regenerate, note it in the changelog).
