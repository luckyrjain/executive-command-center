# Automation Policy Scope Enforcement Design

**Status of this document:** **accepted by the repository owner on 2026-10-02** (PR #359): Decisions 1-6 as recommended, with the answers recorded in "Owner decisions" at the end. It changes no code itself; `docs/phases/phase-005/APPROVAL-POLICY.md`'s "Accepted limitation" section changes in the implementation PR, not here. Implementation may now begin, in Decision 6's order. This document is the "design change of its own" that `APPROVAL-POLICY.md:45` and `backend/ecc/domains/automation/policy.py:62-73` say enforcement needs.

## Outcome

Turn `automation_policies.action_types`, `data_classes` and `value_limit` from stored-only fields into live controls. When this ships, a policy narrows which kinds of registered adapter it authorizes, which data classes those adapters may handle, and how much monetary value a run may move before a human has to approve it. Every authority surface checks the same rules: publish, dispatch, retry-resume, compensation and `/simulate`.

## Why this isn't a green field

- **The fields already exist and round-trip.** Migration `0038_phase5_workflow_schema.py:185-202` declares `action_types`/`data_classes` as `ARRAY(TEXT) NOT NULL DEFAULT '{}'` and `value_limit` as `Numeric(14,2) NOT NULL` (`>= 0`, `:232`). `PolicyCreateRequest` (`policy.py:371-382`) checks shape only: `action_types` is any list of up to 50 strings, `data_classes` any list of up to 20 strings, and both default to `[]`. Neither has a vocabulary. `PolicyPanel.tsx:110-111` builds both lists from free-text comma-separated inputs.
- **Nothing reads them on the dispatch path.** This is stated in `policy.py:50-61`, `APPROVAL-POLICY.md:44`, `DATA-MODEL.md:16`, `API-SCHEMAS.md:47` and `TEST-PLAN.md:23`. `approvals.evaluate_approval_requirement` (`approvals.py:233-268`) reads only `high_impact_categories`, `approval_mode` and `count_limit`.
- **The adapter contract has nowhere to hang the metadata.** `ActionAdapter` (`adapter_contract.py:89-93`) declares `adapter_id`, `input_schema`, `output_schema`, `reversible` and `high_impact_categories`, and nothing else. `AdapterRegistry.register` (`adapter_contract.py:160-176`) validates protocol conformance, uniqueness and the closed `HIGH_IMPACT_CATEGORIES` set (`:67-77`). That last check is the precedent the new fields follow.
- **Six adapters are registered** (`adapters.py:96-107`):

  | adapter_id | file:line | reversible | high_impact_categories | compensate |
  |---|---|---|---|---|
  | `local.create_note` | `local_adapters.py:260,270-271` | True | `{}` (bounded) | no |
  | `local.send_test_notification` | `local_adapters.py:409,414-415` | False | `{person-directed}` | no |
  | `fake.external_action` | `local_adapters.py:492,497-498` | True | `{public}` | yes (`:518`) |
  | `github.add_issue_comment` | `engineering/write_actions.py:311,318-319` | True | `{public}` | no |
  | `gitlab.add_note` | `engineering/write_actions.py:446,449-450` | True | `{public}` | no |
  | `jira.add_comment` | `engineering/write_actions.py:581,584-585` | True | `{public}` | no |

  None of these adapters moves money. `local.create_note` is the only one that a `bounded_recurring` policy can dispatch without a per-run prompt.
- **A closed data-class vocabulary already exists.** Phase 4 defines four classes: `public`, `internal`, `sensitive` and `restricted` (`docs/phases/phase-004/DATA-MODEL.md:25-27`, `backend/ecc/domains/ai_runtime/runtime.py:2232` `DataClass = Literal[...]`). Phase 4 also defaults unclassified records to `sensitive`, and `MODEL-ROUTING-CONTRACT.md:17` evaluates data class first. This design reuses that vocabulary rather than inventing a new one.
- **Policies are immutable and short-lived.** A policy has create and revoke only, with no update endpoint (`DATA-MODEL.md:16`). `expires_at` is always `now + 90 days` and the caller cannot set it (`policy.py:264-266`). There is no renewal path. A run pins `policy_id` from the active version's `policy_ref` at enqueue (`worker.py:1546-1557`). So every existing policy row ages out of usability within 90 days of its creation. Decision 3 depends on this fact.
- **Precedents for each outcome shape already exist:**
  - `count_limit` turns into a `policy-limit-exceeding` approval requirement, not a block (`approvals.py:70-91,268`).
  - An unusable policy becomes `StepBlockedByPolicy(reason)`, which maps to `needs_review` (`worker.py:970-978,3164-3165`).
  - `preview_only` became its own outcome type because its operational meaning differs (`worker.py:981-1006`).
  - Compensation re-checks the policy and fails with `PolicyUnusableDuringCompensation` (`worker.py:2921-2929`).
  - A high-impact compensation `action_ref` is rejected at publish with `422 COMPENSATION_ACTION_REF_HIGH_IMPACT` (`workflows.py:795,1216`).
  - `/simulate` repeats the dispatch gate's ordering step for step (`workflows.py:1450-1540`).

## Decision 1: adapter metadata

**Recommendation.** Add three members to `ActionAdapter` in `adapter_contract.py`:

1. `action_type: str`, a member of a new closed `ACTION_TYPES: frozenset[str]` defined next to `HIGH_IMPACT_CATEGORIES`.
2. `data_classes: frozenset[str]`, a **non-empty** subset of a new `DATA_CLASSES` constant. The constant equals Phase 4's four classes and is asserted equal to `ai_runtime.runtime.DataClass`'s `get_args` by a unit test. It is not imported, so `adapter_contract.py` stays a dependency-free leaf (`adapter_contract.py:5-12`).
3. An optional method `dispatch_value(action_input) -> Decimal`. If an adapter does not define it, its dispatch value is `Decimal("0")`. Like `compensate`, it is checked with `hasattr` (`adapter_contract.py:191-197`).

**Proposed `ACTION_TYPES` (initial closed set) and per-adapter backfill:**

| adapter_id | action_type | data_classes | dispatch_value |
|---|---|---|---|
| `local.create_note` | `note.create` | `{sensitive}` | absent → 0 |
| `local.send_test_notification` | `notification.send` | `{sensitive}` | absent → 0 |
| `fake.external_action` | `fake.external` | `{internal}` | absent → 0 |
| `github.add_issue_comment` | `comment.create` | `{sensitive}` | absent → 0 |
| `gitlab.add_note` | `comment.create` | `{sensitive}` | absent → 0 |
| `jira.add_comment` | `comment.create` | `{sensitive}` | absent → 0 |

**Meaning of `data_classes`:** the highest classes of workspace data the adapter's input may carry or expose. A step's `input_mapping` can pull arbitrary workspace content (`worker._resolve_step`). Phase 4 defaults every domain record to `sensitive`. So any adapter that writes free text must honestly declare `sensitive`. `fake.external_action` carries only an opaque `external_ref` and `payload` used in tests, so it declares `internal`. With the current six adapters this field separates very little: only "fake" from "real". It is still worth enforcing now so that the first adapter carrying `restricted` data, or carrying only `public` data, is already governed. Per-step data classification derived from `input_mapping` provenance is deferred (alternative D1-c).

**Registry-time validation** goes in `AdapterRegistry.register`, alongside the existing category check at `:170-175`. Each failure raises a new `ValueError` subclass:

- `action_type ∉ ACTION_TYPES` → `AdapterActionTypeInvalid`.
- `data_classes` empty or not a subset of `DATA_CLASSES` → `AdapterDataClassInvalid`.
- `"financial" in high_impact_categories` with no `dispatch_value` → `AdapterValueUndeclared`.

**Alternatives considered:**

- **D1-a: use `adapter_id` itself as the action type**, so `action_types` becomes an `action_ref` allowlist. This needs no new vocabulary, and one existing test already stores `["local.create_note"]` (`tests/test_automation_policy_postgres.py`). Rejected as the default because:
  - it cannot group the three comment adapters;
  - every newly registered adapter silently falls outside every existing policy;
  - the column name promises a kind, not an identity.

  It stays a fallback if the owner prefers exactness over grouping. The owner chose the coarse vocabulary (Owner decision 1).
- **D1-b: put metadata in a separate registry table or config file** keyed by `adapter_id`. Rejected because it splits one adapter's declarations across two places. `high_impact_categories` is static and lives on the adapter class, and this design keeps the same "static per adapter, validated at registration" rule (`adapter_contract.py:30-33`).
- **D1-c: classify data per dispatch from the resolved input.** This would be more precise. It is deferred because it needs provenance tracking through `input_mapping` that does not exist, and a wrong runtime classifier would silently fail open.

## Decision 2: fail-closed default for an adapter that declines to classify itself

**Recommendation: there is no default. An unclassified adapter cannot be registered.** `action_type` and `data_classes` are required protocol members. `@runtime_checkable` already makes `register` raise `TypeError` when a member is absent (`adapter_contract.py:161-165`). An empty `data_classes` is rejected as well (Decision 1), so "touches no data" cannot be claimed. The same applies to the test-only fake adapters. About ten test modules define their own (`grep high_impact_categories tests/`), and each gains the two attributes in the implementation PR.

**Alternatives considered:**

- **D2-a: optional members with a conservative default** (`action_type="unclassified"`, `data_classes={"restricted"}`), mirroring Phase 4's "default to `sensitive`". Rejected. An adapter that is unclassified because nobody declared anything would be authorized by any policy whose author listed `restricted` and `unclassified`. A registration error surfaces in CI. A default surfaces in production.
- **D2-b: a registered but non-dispatchable adapter.** Rejected. It adds a fourth adapter state for no benefit over refusing to register.

This is the stricter reading of the `high_impact_categories` precedent. That precedent fails closed only on *unknown* values (`:170-175`), and `APPROVAL-POLICY.md:29` admits it does not stop an adapter from under-declaring. Here, under-declaring is mechanically impossible because the members are required and `data_classes` must be non-empty.

## Decision 3: empty-list semantics and compatibility for existing rows (**Owner: accepted**)

**The risk.** Production rows hold whatever authors typed into free-text fields. That is most likely `[]`, since both the API and the UI default to an empty list. It may also be adapter ids (`local.create_note`) or ad-hoc strings that are not in the new vocabulary. Applying any single rule to those rows when enforcement ships has one of two effects:

- **Silently widen:** treat `[]` or unknown values as allow-all, which is today's behaviour but would then be presented as an enforced scope.
- **Silently break:** treat them as deny-all, so every bounded workflow stops at its next dispatch and lands in `needs_review`.

Neither outcome is acceptable unannounced.

**Recommendation: version the scope semantics per row and grandfather legacy rows until they expire.**

1. Add `automation_policies.scope_enforced boolean NOT NULL`. The migration adds it with `server_default false`, which every existing row receives, then changes the default to `true`. A direct `INSERT` therefore defaults to enforced.
   - Use the next free migration number at implementation time. `0085` is being claimed concurrently by `0085_distinct_approver.py` on another branch.
2. For `scope_enforced = true` rows, add a `CHECK`: `cardinality(action_types) >= 1`, `cardinality(data_classes) >= 1`, and `data_classes <@ ARRAY['public','internal','sensitive','restricted']`. `action_types` vocabulary membership is enforced in the application only, because `ACTION_TYPES` will grow with adapters and a DB-level list would need a migration each time. A parity test pins the `data_classes` list to `DATA_CLASSES`.
3. **New policies** (`POST /automations/policies`):
   - Must list at least one `action_type` and at least one `data_class`, each from the closed vocabularies. `422 POLICY_SCOPE_EMPTY` or `422 POLICY_SCOPE_UNKNOWN_VALUE` otherwise.
   - Enforcement code still treats an empty list as **deny-all**, as defence in depth.
   - Empty means "authorizes nothing", never "authorizes everything", in line with `APPROVAL-POLICY.md:11`'s "least-privilege, explicit".
4. **Legacy rows** (`scope_enforced = false`) keep today's exact behaviour: no scope check. They are flagged on every surface, and they age out by construction within 90 days of creation. `policy.py:264` fixes `expires_at`, and no renewal path exists. After that, the flag and the legacy branch can be removed in a cleanup PR.
5. **Before migrating**, the owner runs a read-only report against production:

   ```sql
   SELECT id, workflow_id, action_types, data_classes, value_limit, expires_at
   FROM automation_policies
   WHERE revoked_at IS NULL AND expires_at > now();
   ```

   This shows exactly which live policies will be grandfathered and when the last one expires. A policy whose author intended a narrow scope can be revoked and recreated under enforcement immediately, through the existing create/revoke endpoints.

**Alternatives considered:**

- **D3-a: enforce from day one with `[]` = allow-all.** Rejected. The field then means "unrestricted" when left blank, which is fail-open, and the UI's blank default would make that the common case.
- **D3-b: enforce from day one with `[]` = deny-all.** Rejected for legacy rows because it silently breaks every existing workflow. This rule is adopted for new rows.
- **D3-c: backfill legacy `[]` to the full current vocabulary.** This preserves effective authority and makes it visible. Rejected because it writes authority the author never chose into an "immutable" policy row. It also still fails on legacy non-empty values that are not in the vocabulary, such as `local.create_note`, which would need a guessed mapping.
- **D3-d: enforce legacy non-empty rows as stored.** Rejected. Free-text values that are not in the vocabulary would deny everything, which is a silent break, and values that happen to match would narrow authority the author was told was not enforced.

## Decision 4: `value_limit` semantics (**Owner: accepted**)

**Recommendation: dispatch value per step, cumulative per run, enforced as `policy-limit-exceeding`.**

- **Per-step value:** `adapter.dispatch_value(validated_input)` if the adapter defines it, otherwise `Decimal("0")`. All six current adapters therefore have value 0.
- **Rule:** a step requires approval when the sum of its run's already-dispatched step values plus its own value exceeds `value_limit`. Because `value_limit >= 0` (`0038:232`), a value-0 step can never exceed it. **Value enforcement is therefore a no-op for every adapter that exists today.** Its first real coverage is a test-only financial fake adapter.
- **Persistence:** add `workflow_run_steps.dispatch_value numeric(14,2) NULL`, written in the same `INSERT` that records the `dispatched` row. The run sum is one `SUM()` in the query that already counts steps (`worker._count_dispatched_action_steps`, `worker.py:1954`). Storing the value, rather than recomputing it from `input_mapping`, keeps the sum stable even if an adapter's `dispatch_value` changes between deploys.

**Reconciling "per policy per day."** `APPROVAL-POLICY.md:38` and design Decision 6 say "monetary value / action count per policy per day", but `count_limit` is already implemented **per run** (`approvals.py:83-91`; `policy.py:40-41`). This design applies the same per-run window to `value_limit`, so the two limits behave alike. It also corrects the table row to "per run" for both, with "per policy per day" recorded as deferred.

**Alternative D4-a, per policy per day,** would need four things:

- a cross-run `SUM` over `workflow_run_steps JOIN workflow_runs ON policy_id` inside a day window;
- a timezone choice for "day";
- serialization of concurrent runs against the same budget (row lock on the policy, or an advisory lock);
- a matching change to `count_limit`.

That is a real budget ledger. It is deferred until an adapter with a nonzero value exists to justify it.

**Alternative D4-b, defer `value_limit` entirely.** This would keep the "accepted limitation" text for one field. It is acceptable if the owner prefers. The cost of including it is small: one optional method, one column and one comparison, and including it closes the documented gap completely.

## Decision 5: where and how scope is enforced

**Outcome for an out-of-scope step: block, not approval.**

**Recommendation.** Add two reasons to `StepBlockedByPolicy.reason` (`worker.py:978`): `action_type_not_authorized` and `data_class_not_authorized`. Add the same two to `workflows.PolicyBlockReason` (`workflows.py:1380`). Both map through the existing branch to `needs_review` (`worker.py:3164-3165`).

The rule: a step is in scope when `adapter.action_type ∈ policy.action_types` and `adapter.data_classes ⊆ policy.data_classes`. It is evaluated only when `policy.scope_enforced`.

**Alternatives considered:**

- **D5-a: route it through approval as `policy-limit-exceeding`.** Rejected. A per-run approval would then widen the policy's authority, which makes scope advisory. Approval is consent *within* authority (`APPROVAL-POLICY.md:11`). `value_limit` is different: it is a limit inside the authority, so it keeps using `policy-limit-exceeding` as `APPROVAL-POLICY.md:27` literally defines.
- **D5-b: a new outcome type and terminal run status** (`scope_blocked`), as `preview_only` has. Rejected. `StepBlockedByPreviewOnlyPolicy` exists because preview means "nothing is wrong" (`worker.py:990-1004`). A scope mismatch means "this policy does not authorize this step", which is the same operational meaning as the existing three reasons: an operator must investigate. Adding Literal members keeps every `isinstance` call site correct, whereas a new type would require touching each one.

**Enforcement points.** All of them share one pure helper, `approvals.evaluate_policy_scope(adapter, policy) -> ScopeReason | None`, so dispatch and simulate cannot drift apart. It lives in `approvals.py` because `workflows.py` cannot import `worker.py` (`API-SCHEMAS.md:41`).

1. **Publish time** (new, follows the `COMPENSATION_ACTION_REF_HIGH_IMPACT` precedent at `workflows.py:795,1216`):
   - `activate_workflow_version` rejects any action or compensation step whose `action_ref` is out of scope for the version's `policy_ref`. Response: `422 ACTION_REF_OUTSIDE_POLICY_SCOPE` with `{step_id, action_ref, reason}`.
   - Because a policy is immutable and pinned, this catches nearly every mismatch while authoring.
   - Legacy (`scope_enforced=false`) policies are exempt.
2. **First dispatch,** in `worker._evaluate_dispatch_gate` (`worker.py:2073-2145`):
   - Runs after `_resolve_usable_policy` and adapter resolution (`:2126-2131`), and **before** `_evaluate_approval_gate`. An unauthorized step therefore never creates an `approval_requests` row that a human could approve.
   - Runs before the `preview_only` exit, so a preview run also reports scope violations.
   - An unregistered adapter is skipped here, and existing handling applies.
3. **Retry-resume** (`worker.py:2326-2328`): the same helper runs after `_resolve_usable_policy`. This is defence in depth against an adapter being reclassified by a deploy that lands during a backoff window, matching that block's own "re-check on resume" reasoning (`:2296-2313`).
4. **Compensation** (`worker.py:2921-2929`):
   - When the compensation step's own `action_ref` adapter is dispatched through `execute()`, it is scope-checked after `_compensation_policy_usable`.
   - On failure, `error_class='PolicyScopeViolationDuringCompensation'` and the run ends `compensation_failed`, mirroring `PolicyUnusableDuringCompensation`.
   - The original adapter's own `compensate()` is not re-checked. It undoes an action the gate already authorized, and denying the undo would leave the side effect behind.
   - `value_limit` is not checked here, because compensation adapters cannot be `financial`: high-impact compensation is rejected at publish.
5. **`/simulate` parity** (`workflows.py:1520-1537`):
   - The same helper is inserted at the same position: after usability and adapter resolution, before `evaluate_approval_requirement`. It yields `dispatch_gate="policy_blocked"` with the new reasons.
   - The walk also accumulates `dispatch_value` exactly as it tracks `action_step_count_so_far`.
   - `SimulateStepResult` gains `action_type`, `data_classes` and `dispatch_value`. These are static declarations, as `reversible` and `high_impact_categories` already are (`workflows.py:1429-1438`).

**Value enforcement** extends `evaluate_approval_requirement` with a required `run_value_so_far: Decimal` keyword. Like the count, it is required so that a forgotten argument cannot fall through to 0 (`approvals.py:247-251`). It returns `True` when `run_value_so_far + step_value > policy.value_limit`.

**Adjacent gap, flagged for the owner.** `_evaluate_approval_gate` stores `adapter.high_impact_categories` on the approval row (`worker.py:2062`). For a count- or value-triggered approval on a bounded adapter, that set is empty, so the approver is never told the reason is `policy-limit-exceeding`. The recommendation is for `evaluate_approval_requirement` to return the effective category set, adding `policy-limit-exceeding` when a limit tripped, and to persist that set instead. **In scope for the implementation PR** (Owner decision 5).

**Error and API surfacing:**

| Surface | Change |
|---|---|
| `POST /automations/policies` | `422 POLICY_SCOPE_EMPTY`, `422 POLICY_SCOPE_UNKNOWN_VALUE` (`{field, values, allowed}`), for new rows only |
| `PolicyResponse` | adds `scope_enforced: bool` |
| `GET /automations/adapters` (new, read-only) | `{adapter_id, action_type, data_classes, reversible, high_impact_categories, has_dispatch_value}` per registered adapter, so the UI can offer the closed vocabularies instead of free text |
| `POST .../publish` | `422 ACTION_REF_OUTSIDE_POLICY_SCOPE` |
| `/simulate` | new `policy_block_reason` values, plus `action_type`, `data_classes` and `dispatch_value` per step |
| Run outcome | `needs_review`; an audit-outbox event `automation.step_blocked` carries `{run_id, step_index, reason}`, because a blocked step writes no step row (`worker.py:970-975`) and the reason is otherwise unrecoverable for an operator (`IMPLEMENTATION-STATUS.md:99`, judgment call 3) |
| `PolicyPanel.tsx` | replaces the two free-text inputs (`:196-199`) with checkbox groups fed by `/automations/adapters` and the four data classes; shows a "Legacy scope: not enforced, expires {date}" badge for `scope_enforced=false` |
| `RunWorkspace` / approval card | shows the block reason; shows `policy-limit-exceeding` when present |

## Decision 6: rollout, tests, and documentation

**Rollout.** One implementation PR, with no feature flag. The per-row `scope_enforced` column is the rollout control: legacy rows are untouched and new rows are enforced. Order of work inside the PR:

1. contract and registry validation;
2. adapter backfill (all six adapters and every test fake);
3. migration;
4. `evaluate_policy_scope` plus value accumulation;
5. gate, retry and compensation;
6. publish check;
7. `/simulate`;
8. API and UI.

Before merging, run the Decision 3 report query against production and include the result in the PR. A cleanup PR after the last legacy row's `expires_at` drops the legacy branch and makes the `CHECK` unconditional.

**Tests** (Postgres, `tests/test_<domain>_<feature>_postgres.py`):

- `tests/test_automation_adapter_metadata_postgres.py`:
  - registration rejects an unknown `action_type`, an empty or unknown `data_classes`, and a `financial` adapter without `dispatch_value`;
  - the six production adapters register with the table values in Decision 1;
  - a `DATA_CLASSES` ↔ `ai_runtime` `DataClass` parity test.
- `tests/test_automation_policy_scope_postgres.py`:
  - create rejects empty or unknown scope values (422);
  - the DB `CHECK` rejects an enforced row with empty scope via direct `INSERT`;
  - the migration leaves existing rows `scope_enforced=false` and new rows `true`;
  - a legacy row dispatches exactly as before.
- `tests/test_automation_scope_enforcement_postgres.py`:
  - an out-of-scope action type or data class is blocked before any `approval_requests` row exists, and the run lands in `needs_review` with an audit event;
  - an in-scope step is unchanged;
  - the retry-resume path re-checks scope;
  - a compensation `execute()` that is out of scope gives `PolicyScopeViolationDuringCompensation` / `compensation_failed`;
  - an original adapter's `compensate()` is not blocked;
  - `preview_only` with an out-of-scope step blocks on scope.
- `tests/test_automation_value_limit_postgres.py`, using a test-only `financial` fake with `dispatch_value`:
  - the run sum crossing `value_limit` requires approval (`policy-limit-exceeding`) even under `bounded_recurring`;
  - value 0 never trips;
  - `dispatch_value` is persisted on the step row.
- Extend `tests/test_automation_simulate_postgres.py`: for the same graph and policy, `/simulate` reports the same block reason and approval points as real dispatch, one assertion per new reason.
- Extend `tests/test_automation_workflows_postgres.py` with `ACTION_REF_OUTSIDE_POLICY_SCOPE` at publish, covering both action and compensation steps.
- Frontend: `PolicyPanel.test.tsx` covers the checkbox vocabularies and the legacy badge.

**Documentation updates in the implementation PR** (each flips "stored but not enforced" to the enforced description, scoped to `scope_enforced` rows):

- `docs/phases/phase-005/APPROVAL-POLICY.md`:
  - rewrite the `:40-47` "Accepted limitation" section as "Scope enforcement", including the legacy-row grandfathering rule and its expiry bound;
  - in the `:38` table row, change "per policy per day" to "per run" for both count and value, with "per day" deferred;
  - drop the `value_limit` "not enforced" clause.
- `backend/ecc/domains/automation/policy.py:25-81` docstring: move the three fields to the enforced list.
- `approvals.py:70-80`: `policy-limit-exceeding` uses count and value.
- `adapter_contract.py:26-43`: the contract shape gains the three members.
- `docs/phases/phase-005/DATA-MODEL.md:16`, plus `scope_enforced` and `workflow_run_steps.dispatch_value`.
- `docs/phases/phase-005/API-SCHEMAS.md:41,47,59`: the new codes, the new endpoint and the simulate fields.
- `docs/phases/phase-005/TEST-PLAN.md:23`: replace "Not covered, because not enforced" with the tests above.
- `docs/phases/phase-005/IMPLEMENTATION-STATUS.md:287` (Gap 3): mark it resolved.
- `docs/phases/phase-006/CONNECTOR-CONTRACT.md:114`: the "never enforced" parenthetical becomes the enforced `comment.create` / `sensitive` scope.

## Owner decisions (2026-10-02)

1. **Action-type granularity:** the coarse closed vocabulary (`note.create`, `notification.send`, `comment.create`, `fake.external`). D1-a (exact `adapter_id` allowlist) is not adopted.
2. **Legacy compatibility:** grandfather live policies (`scope_enforced = false`) until they expire, at most 90 days. No revoke-and-recreate at deploy. New and renewed policies are enforced from the first dispatch.
3. **`value_limit` window:** per run, matching `count_limit`. The implementation PR corrects `APPROVAL-POLICY.md`'s "per policy per day" row to "per run" for both limits. The per-day ledger (D4-a) stays deferred until an adapter with a nonzero value exists.
4. **Data classification:** as proposed. `sensitive` for `local.create_note`, `local.send_test_notification` and the three connector comment adapters; `internal` for `fake.external_action`.
5. **Approval category accuracy:** yes, in the same implementation PR. Count- and value-triggered approvals record `policy-limit-exceeding` on the approval row.
6. **Connector targets:** out of scope. Per-actor connector authorization (#351) covers misuse today. A per-policy connector allowlist would need its own column and design.

## Completion boundary for this planning pass

Complete: the owner has accepted Decisions 1-6 and answered the open questions above. The implementation PR follows Decision 6's order and test list. It uses the next free migration number at that time (`0085` is taken by `0085_distinct_approver.py`, PR #361).
