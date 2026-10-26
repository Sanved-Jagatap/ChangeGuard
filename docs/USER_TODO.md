# Your task list (things only you can do)

Everything else in this repository is already written and tested. This list is ordered. Each item says **what to do**, **what "done" looks like**, and **what to send back to me** if it fails.

**What I could not run in my build environment** (so please treat these as unverified until you do section 2): `ruff`, the FastAPI server, the Streamlit UI, Docker/Postgres, and any live LLM API (no internet there). Everything else (97 tests, 106-case benchmark with mock models) ran and passed.

---

## 1. Local setup (about 15 minutes)

```powershell
cd changeguard
python -m venv .venv ; .\.venv\Scripts\Activate.ps1      # Python 3.11 or newer
pip install -e ".[all]"
python -m pytest -q
changeguard init-db
changeguard investigate CMP-104 --runner baseline
changeguard bench --runner baseline --subset smoke --min-success 0.9
```
**Done when:** pytest shows `95 passed, 2 skipped` (the 2 are Postgres) and the benchmark prints `11/11`.
**If not:** send me the first error block.

## 2. Verify the parts I could not run

- [ ] **2a Lint.** `ruff check . --fix` then `ruff check .`. I checked unused imports by hand, but ruff may find more. Commit the fixes. (CI runs ruff and will fail otherwise.)
- [ ] **2b API.** `uvicorn changeguard.api.main:app --reload`, open `http://localhost:8000/docs`, `GET /health`, then `POST /changes` with header `Idempotency-Key: t1` and the body from `/scenarios`, then `POST /changes/{id}/runs` with `{"runner":"baseline"}`. Anonymous role is `viewer`, so for POSTs set header `X-API-Token: change-me-analyst`.
- [ ] **2c UI.** In a second terminal: `streamlit run src/changeguard/ui/app.py`. Paste `change-me-reviewer` into the sidebar token. Run scenario 1 with runner `baseline`, open the Trace and Proposed action tabs, approve and execute. Expected: verification **passed**.
- [ ] **2d Postgres.** `docker compose up -d db`, then in PowerShell:
  `$env:CG_TEST_POSTGRES_URL="postgresql://cg_admin:cg_admin_dev@localhost:5434/changeguard"; python -m pytest tests/test_postgres.py -q`
  This drops the public schema of that database (it is the throwaway compose DB). Expected: 2 passed. This is the only test of the Postgres roles and triggers.
- [ ] **2e Full stack.** `docker compose up --build`, UI at `http://localhost:8501`.

**Send me:** the traceback or HTTP error text for anything that fails. These are the parts most likely to need a small fix.

## 3. Run a REAL model (the single most important step)

Until this is done, the portfolio has no model results. The mock rows are plumbing only.

1. **Get a key.** Google AI Studio → "Get API key" (free tier, no billing needed) or an OpenAI-compatible key. Put it in `.env` (never commit it). Note that Google says free-tier requests may be used to improve its products. The data here is synthetic, so that is fine.
2. **Pick a model that supports function calling.** I do not know which model names are current in your region and date, so look at the provider's model list. Set `CG_LLM_PROVIDER=gemini` and `CG_LLM_MODEL=<name>` in `.env`.
3. **Check your quota.** Google no longer publishes per-model free limits on its docs page; your limits are shown per project in the AI Studio rate-limit console. A full benchmark run is roughly 1,000-1,700 model calls (about 10-16 turns per case), which may exceed a free daily limit. That is why the runner supports `--sleep` and `--resume`.
4. **Live smoke test:**
   `changeguard investigate CMP-104 --runner agent --provider gemini --model <name>`
   Expected: `status: completed`, outcome `recommendation`, CMP-109, and a trace with several tool calls. **If it fails (HTTP 400, schema errors, no tool call), send me the full error output.** The adapters were tested only against fixtures I wrote from the documented formats, never against the live API, so a small fix is plausible.
5. **Run in stages** (each writes a result file and can resume):
   ```powershell
   changeguard bench --runner agent --provider gemini --model <name> --subset smoke --sleep 6 --verbose
   changeguard bench --runner agent --provider gemini --model <name> --subset dev --sleep 6 --resume --out benchmarks/results/gemini_dev.json
   changeguard bench --runner agent --provider gemini --model <name> --subset heldout --sleep 6 --resume --out benchmarks/results/gemini_heldout.json
   ```
   If you hit a rate limit mid-run, wait and re-run the same command with `--resume`.
6. **Discipline:** look at `dev` failures and improve prompts/tools if you wish. **Do not tune against the held-out split.** Run held-out once at the end with the final prompt, and report both numbers.
7. **Cost:** to report cost, set `CG_PRICING_JSON={"<model name>": {"input_per_mtok": <price>, "output_per_mtok": <price>}}` using the provider's current price page. If you do not set it, the report says "n/a" (I never guess prices).
8. **Publish:** copy the final result JSONs into `benchmarks/published/`, then
   `changeguard report benchmarks/published/*.json --out docs/RESULTS.md`, and update the Results table in `README.md` with the real numbers. Keep the mock rows, clearly labelled, or remove them. Also change the README "Honest status" rows for the adapters and real-model results.

**Done when:** `docs/RESULTS.md` has a row for a real model with a 95% interval, dev and held-out reported separately.

## 4. A second comparison (recommended)
Repeat section 3 with a different model (a smaller/cheaper one vs a larger one). This gives you the "quality vs cost vs latency" trade-off the brief asks for. Same commands, different `--model`.

## 5. Failure analysis
Copy `docs/FAILURE_ANALYSIS_TEMPLATE.md` to `docs/FAILURE_ANALYSIS.md` and fill in 5-10 real failures with their trace evidence. If you decide the *benchmark* was wrong about a case, fix `bench/oracle.py` or `bench/generate.py`, run `changeguard bench-generate`, commit the new dataset, and note the change. This file is what interviewers will read most closely.

## 6. Observability (optional but strong)
The built-in trace (UI timeline + `agent_steps` table) already satisfies tracing. To also show it in a standard tool: run Arize Phoenix locally
(`docker run -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest`), `pip install -e ".[otel]"`, set `OTEL_EXPORTER_OTLP_ENDPOINT` to Phoenix's OTLP/HTTP traces URL, run an investigation, and screenshot the spans. The bridge is untested; if it errors it is silently disabled and the run still works.

## 7. Public deployment
1. **GitHub:** create a repository, push, confirm the `ci` workflow is green. Do **not** commit `.env`.
2. **Database:** create a free hosted Postgres (for example Neon). Copy the admin connection string.
3. **Initialise it once** (from your machine): set `CG_DATABASE_URL` to the admin string and run `changeguard init-db`. This migrates, seeds and creates the least-privilege roles. Role passwords default to dev values. For a public deployment set `CG_PG_READER_PASSWORD`, `CG_PG_RECORDER_PASSWORD`, `CG_PG_SANDBOX_PASSWORD` to long alphanumeric values first, and build the three role connection strings from them.
4. **Host:** create two services from the repo's Dockerfile on Render/Fly/Railway (or use Streamlit Community Cloud for the UI): the API (default command) and the UI (`streamlit run src/changeguard/ui/app.py --server.port $PORT --server.address 0.0.0.0`, with `CG_API_URL` set).
5. **Environment** (see `docs/DEPLOYMENT.md`): all three `CG_DATABASE_URL_*`, provider + key, **new random `CG_DEMO_TOKENS`**, `CG_MAX_AGENT_RUNS_PER_DAY`, `CG_ALLOW_RESET=1` (sandbox only).
6. **Spending protection:** set a hard budget/alert in the provider console. Decide section 10, question 1 first.
7. Open the URL, run the three scenarios, put the link at the top of `README.md`.

## 8. Demo assets
- 2-3 screenshots (report, trace timeline, approval panel) and a short GIF of scenario 1 plus an approval. Put them in `docs/img/` and embed in the README.
- Replace every "not deployed yet" / "not run yet" in the README once true.

## 9. Resume and interview
- Write resume bullets **only from numbers you measured**. Pattern: "Built an LLM agent with tool calling and human-approval guardrails for engineering-change impact analysis; benchmarked on 106 cases vs a fixed pipeline (success X% vs Y%, held-out Z%), 0 unauthorised writes, traced end to end; deployed at <URL>."
- Fill the blanks in `docs/INTERVIEW.md` and rehearse the two-minute version out loud.

## 10. Decisions only you can make (my defaults in brackets)
1. Should anonymous visitors be able to start hosted-model runs? [No: keep `CG_ANONYMOUS_ROLE=viewer`; give recruiters an analyst token in your application, or let them use the baseline runner only.]
2. Which provider/model(s) to benchmark? [One small and one larger model from the same provider.]
3. Keep the mock rows in `RESULTS.md`? [Yes, clearly labelled: they show the guards hold even against a fooled model.]
4. SQLite or Postgres for the public demo? [Postgres, because the least-privilege roles are part of the story.]
5. Rename the project/domain? [Optional; the code uses generic names.]

## 11. Do not
- Commit `.env`, real API keys, or real company data.
- Tune prompts on the held-out split.
- Describe mock results as model results, or claim anything in the README that you have not measured.
- Present "eligible for review" as "compliant" or "equivalent".

## What to bring back to me
Section 2 errors, the live smoke test output (section 3 step 4), the result JSONs when you have them, and any case where you think the benchmark label is wrong. I can then fix adapters, tune prompts on dev, extend the benchmark, or help write the failure analysis.
