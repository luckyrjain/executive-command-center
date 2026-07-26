---
id: PHASE-005-UX-STATES
title: Phase 5 Automation UX States
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 5 UX States

Primary surfaces: workflow builder, simulation, policy review, approval inbox and run history. Clearly distinguish proposed, approved, active, paused and revoked. Show next scheduled run, authority scope and side effects.

Required states include waiting approval, expired approval, policy revoked, paused, retrying, unknown external outcome, partially compensated and kill switch active -- mapped directly to `DATA-MODEL.md`'s resolved run/step states (`waiting_approval`, `expired`, `needs_review`/`unknown`, `compensation_failed`, `rate_limited`). A run in `needs_review` (an `unknown` step outcome, `EXECUTION-CONTRACT.md`) surfaces as a distinct, non-dismissible state requiring the operator to inspect the target system and explicitly resolve it (`docs/runbooks/PHASE-5-RECOVERY.md`'s "What an operator does" section) -- it is never presented as a transient/retrying state, since automatic resolution is exactly what this state exists to prevent.

**Wiring notes, Task 6 (backend now real; this remains a backend-only task -- no frontend surface ships here, per its own scope boundary).** "Retrying" is realized as a `queued` run whose *current step's own row* shows `workflow_run_steps.status = 'retrying'` (`attempt_count`/`error_class` visible via `GET /automations/runs/{id}`'s step detail) -- there is no run-level `'retrying'` status (`DATA-MODEL.md`). "Partially compensated" is realized as a run in `compensation_failed` whose `compensation_steps` ledger shows a mix of `succeeded`/`failed` rows -- exactly the acceptance criterion "partial compensation is visible and recoverable." "Kill switch active" is realized as: a `needs_review` run (a kill switch discovered mid-dispatch, `process_claimed_run`'s per-step check) plus, separately, a `409 kill_switch_active` response from `POST /automations/runs` for any *new* run attempt against a killed workflow.

**Wiring notes, Task 7a (backend now real; still backend-only -- no frontend surface ships here, a later task builds against these three endpoints).** Both gaps Task 6 left named above are now closed by real endpoints, not only a direct DB read: "partially compensated" is `GET`-able via `RunDetailResponse.compensation_steps` (`GET /automations/runs/{id}`, `worker.list_compensation_steps` wired into `runs.py`) rather than `list_compensation_steps` called from non-HTTP code only; "kill switch active" is `GET`-able via `GET /automations/workflows/{id}/kill_switch` (current effective state -- global/per-workflow -- plus this workflow's own history) rather than `kill_switches.list_kill_switches` called from non-HTTP code only, so a future UI can genuinely show an operator *why* new runs are rejected before attempting one, not only after. **Simulation** (`API-SCHEMAS.md`'s `/simulate`, this section's own next paragraph) is also now real: `POST /automations/workflows/{id}/simulate` returns, per step, the adapter's declared preview/`reversible`/`high_impact_categories`, the step's own `compensate_ref` target if any, and a `dispatch_gate` genuinely re-evaluated against the version's own pinned policy -- the data a "simulation results are visually distinct from real run history" UI (this section's next paragraph) would render against.

Destructive/high-impact approvals (`APPROVAL-POLICY.md`'s taxonomy) require deliberate confirmation: the exact target, payload summary, high-impact category, reversible status and expiry are shown before the approve action is reachable, and the approval UI never pre-fills or auto-submits a decision. Simulation results (`API-SCHEMAS.md`'s `/simulate`) are visually distinct from real run history at every surface -- a simulated run can never be mistaken for one that produced real side effects, matching the acceptance criterion "simulation never causes side effects" being visible, not just true.

All flows are keyboard accessible and meet WCAG 2.2 AA.
