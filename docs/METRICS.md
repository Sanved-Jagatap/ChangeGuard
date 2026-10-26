# Governed metric definitions (version 1.0)

All numbers shown to users come from `domain/metrics.py`. The model may quote them; it cannot redefine them.

| Metric | Definition | Unavailable when |
|---|---|---|
| affected_count | distinct ancestors (assemblies + products) of the component in the BOM, within `depth_limit` (default 8, max 12). `truncated=true` is reported if the limit cut paths | component unknown |
| qty_per_unit | for a top-level item (no parents): sum over every BOM path of the product of `qty_per` along the path | |
| exposure_units | sum over top-level items of `planned_units` (optionally one site) x `qty_per_unit` over the demand-plan horizon (90 days in the seed) | status `partial` = some top-level items have no demand rows, so the number is a lower bound; `unavailable` = none have |
| available_units | sum over sites of `max(0, on_hand - reserved)` | no inventory rows (reported as unavailable, never as 0) |
| shortage_units | `max(0, exposure_units - available_units)` | either side unavailable |
| coverage_ratio | `available_units / exposure_units` | exposure is 0 or unknown |
| eligibility | `blocked` if any blocking reason; else `insufficient_evidence` if any gap; else `eligible_for_review` | |

Worked example, CMP-104 (hall sensor), no site filter: EB100 650 units x 3 = 1,950; EB200 200 units x 6 = 1,200 (two drive units per cargo bike x 3 sensors); spare kit 150 x 3 = 450 → **exposure 3,600**. Available 800 + 600 = 1,400 → **shortage 2,200**, coverage 0.3889.

## Eligibility reasons
Blocking: `not_declared_alternate`, `supplier_inactive` (no active supplier), `lead_time_exceeds_constraint`, `compliance_failed`.
Evidence gaps: `no_supplier_records`, `missing_lead_time`, `compliance_not_checked`, `compliance_unresolved`, `data_conflict` (conflicting records), `stale_evidence` (older than 730 days or undated).
`eligible_for_review` means a human should look at the candidate. It does **not** mean compliant, equivalent or approved.

## Compliance status per requirement
`confirmed` (all records confirmed and fresh) / `failed` / `unresolved` (pending, stale, or conflicting) / `not_checked` (no record). Market `null` means every modeled requirement; a market with no modeled requirements yields `compliance_not_checked`.
