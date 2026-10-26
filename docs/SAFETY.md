# Safety model

Each row names the control **and the test that exercises it**. Tests marked (guard stress) use a simulated gullible model.

| Threat | Control | Test |
|---|---|---|
| Model tries to execute/release something | write tool is never in the tool list; calling it (or any unknown tool) is blocked, counted and traced | `test_loop.test_blocks_write_tool_and_counts_it`, `test_tools.test_write_tool_never_exposed_or_runnable` |
| Model tries to relax market/lead-time limits | tool schemas reject those fields; limits are read from the stored request | `test_tools.test_model_cannot_override_constraints` |
| Model recommends a blocked or unevaluated candidate | report enforcement, one retry, then override to escalation | `test_loop.test_recommending_*` , `test_bench.test_guards_hold_against_a_gullible_model` (guard stress) |
| Prompt injection in documents, descriptions or the request | documents flagged and wrapped as untrusted data; system prompt rules; the controls above limit what injected text can cause | benchmark family ADV; guard stress run |
| Draft proposal for an unsafe action | proposals validated against business rules (eligible candidate only, own component only, stock available, max 2 per run) | `test_tools.test_draft_*`, `test_stock_hold_rules` |
| Approved payload altered afterwards | approval stores the payload hash; execution recomputes and compares | `test_approvals.test_payload_tampered_after_approval_is_blocked` |
| Reviewer approves something other than what they saw | approval must present the hash shown on screen | `test_approvals.test_approval_binds_to_exact_payload_hash` |
| Revision smuggles in an ineligible candidate | revision creates a new proposal that passes the same pre-checks; old approvals never apply | `test_revision_*` |
| Stale approval (world changed) | pre-checks re-run at execution; failure is recorded, nothing applied | `test_stale_state_blocks_execution` |
| Double execution / replay | one execution row per action enforced by a unique key; replay returns the original | `test_happy_path_*`, `test_second_exec_row_is_impossible_at_db_level` |
| Unauthorised user | role checks on decide/execute/submit; denials are audited | `test_roles_are_enforced_and_denials_audited` |
| Silent verification failure | verification re-reads state; a mismatch sets `verification_failed` | `test_verification_failure_is_reported_not_hidden` |
| Audit tampering | triggers block UPDATE/DELETE even for admin | `test_db_roles.test_audit_is_append_only_even_for_admin` |
| Tools damage data | reader role is read-only at the connection level | `test_db_roles.*` |
| Runaway loops / cost | step, token, wall-clock caps; daily hosted-run cap; per-IP rate limit | `test_loop.test_max_steps_*`, `test_token_budget`, `test_wall_clock`, `test_service.test_daily_budget_*` |
| Fabricated evidence in the report | claims must cite a successful step containing subject and value | `test_loop.test_good_recommendation_accepted_with_verified_claims` |
| Provider errors / malformed output | LLM errors end the run cleanly; unparseable tool arguments are rejected; keys never echoed | `test_loop.test_llm_error_fails_cleanly`, `test_llm_adapters.*` |
| Secrets in logs | keys only from environment; error text excludes headers | `test_http_gives_up_and_never_echoes_headers` |

## Not covered (be honest in interviews)
- Free-text summaries can still be swayed by hostile text. Decisions are protected, prose is not.
- Claim verification is substring-based.
- Demo authentication is static tokens; there is no per-user data scoping or SSO.
- A hung database statement cannot be killed by the thread timeout; configure a Postgres `statement_timeout` for the reader role.
- PostgreSQL roles/triggers are written but were not executed by the author. Run `tests/test_postgres.py`.
- No red-team campaign beyond the benchmark's ADV/UNS families has been run against a real model.
