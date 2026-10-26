# Architecture

## Principles
1. **The model chooses the next step; code owns every consequence.** Tools are read-only except one that creates a *draft proposal*.
2. **Constraints belong to the request, not the model.** Target market, lead-time limit and site are read from the stored change request inside the tools; the tool schemas reject those fields if a model sends them.
3. **Absence is not zero and unresolved is not compliant.** Missing stock/demand/lead-time data is reported as `unavailable`/`partial`; only `confirmed` evidence counts.
4. **Every action is explicit, bound to a hash, idempotent and verified.**

## Request lifecycle
1. `POST /changes` (needs `Idempotency-Key`): intake validated against a strict schema (ID formats, enums, length limits, unknown fields rejected), component/site existence checked, change stored, audit event written.
2. `POST /changes/{id}/runs`: a run row is created; the loop starts with a system prompt, the request, and the tool list (without write tools).
3. Each model turn: limits are checked (steps, tokens, wall clock), the model is called, the turn and token usage are traced. Each tool call is validated against its schema, executed on a worker thread with a timeout, retried only for retryable failures (timeout, temporary DB error, empty/malformed result) up to a cap, and every attempt is traced.
4. The model ends with `submit_report` (structured, schema-validated) or `escalate_to_human`.
5. Code verifies the report: a recommendation must name a candidate that `calculate_impact_metrics` evaluated **in this run** and rated `eligible_for_review`; assessments must match the deterministic result; claims are checked against the cited steps. Violations are bounced back once, then the report is **overridden** to an escalation.
6. If the run drafted a proposal, a reviewer approves or rejects the exact payload hash; execution re-checks everything against current state, applies it with the narrow writer role in one transaction, then verifies by re-reading.

## Data roles
| Role | Can | Used by |
|---|---|---|
| reader | SELECT only (SQLite: opened read-only at file level; Postgres: SELECT grants) | all agent tools |
| recorder | write workflow state: changes, runs, steps, proposals, approvals, audit | tracing, approvals |
| sandbox_writer | insert draft change orders, increase `inventory.reserved`, record executions, insert audit | executor only |
| admin | migrations, seeding, reset | setup |

`audit_events` and `approvals` cannot be updated or deleted by anyone (trigger), including admin.

## Schema (abridged)
`components, bom_links, component_alternates, suppliers, component_suppliers, inventory, demand_plan, sites, compliance_requirements, compliance_records, documents` (engineering data) and
`engineering_changes, agent_runs, agent_steps, proposed_actions, approvals, executed_actions, change_orders, audit_events` (workflow).
CHECK constraints reject negative stock, non-positive BOM quantities, self-referencing BOM links, invalid enums.

## Tracing
`agent_steps` stores every LLM turn, tool attempt, guard block and system event with input/output JSON, latency, tokens and error kind; the UI renders it as a timeline. If `OTEL_EXPORTER_OTLP_ENDPOINT` is set and the OpenTelemetry packages are installed, the same steps are emitted as spans (for example to Arize Phoenix). The OTel bridge is untested.

## Provider layer
`LLMClient.generate(system, messages, tools) -> LLMTurn` with Gemini and OpenAI-compatible implementations over plain HTTPS (retry/backoff on 429/5xx; keys never logged). Gemini assistant parts are replayed verbatim so any provider-specific reasoning signatures survive multi-step tool use. Cost is computed only when a price table is configured; otherwise it is reported as unavailable.

## Baseline
`baseline.py` runs the same tools in a hard-coded sequence with the same limits and produces the same report shape. It never reads free text, which makes it immune to injected text and also unable to reason about paraphrased requests.
