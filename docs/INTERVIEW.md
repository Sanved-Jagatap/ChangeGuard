# Interview kit

## Two-minute explanation (fill the blanks only with numbers you measured)
"ChangeGuard handles engineering change requests, for example a discontinued part. An LLM agent investigates through tools: it finds every assembly and product that uses the part,
checks stock and demand exposure, supplier options and compliance evidence, and writes a report. What makes it safe is the split of responsibilities: the model only reads and drafts;
deterministic code computes the metrics, applies the hard constraints and decides which candidates are even eligible; and a human approves the exact payload hash before anything runs.
I built a 106-case benchmark with an independent oracle, covering failures, missing data, prompt injection and unsafe requests, and compared the agent with a fixed pipeline.
With [MODEL] the agent scored [X]% vs [Y]% for the pipeline; the cases it won were [..] and the ones it lost were [..]. It's traced end to end, deployed at [URL]."

## Questions to expect
- **Why an agent and not a pipeline?** Because some situations need adaptive behaviour (very deep BOMs that report `truncated`, paraphrased unsafe requests, different evidence gaps). The baseline is in the repo so the claim is measured, not asserted. Say honestly where the pipeline was as good.
- **What stops the model doing damage?** Walk through `docs/SAFETY.md`: no write tool in its toolbox, constraints read from the request, report enforcement with override, hash-bound approvals, narrow DB roles.
- **How do you know the benchmark labels are right?** Independent oracle, hand-verified numbers (CMP-104: 3,600 / 1,400 / 2,200), and the limitation that it encodes the written rules.
- **What would you do with real data?** Replace the synthetic loader with PLM/ERP connectors, add SSO and per-user scoping, DB statement timeouts, a much larger labelled set from historical changes.
- **What failed?** Have 3 specific failure cases ready from `docs/FAILURE_ANALYSIS_TEMPLATE.md` once you have real-model results.
- **Cost and latency?** Quote measured per-run tokens, p50/p95, and cost only if you configured prices.
