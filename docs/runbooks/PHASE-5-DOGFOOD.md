# Phase 5 Dogfood Validation Record

## Purpose

`docs/phases/PHASE-005-automation.md`'s own Exit criteria name a required,
real-world validation step separate from and in addition to every automated
CI/acceptance gate: "Staged dogfood advances from preview to bounded actions
with zero unauthorized effects." This document is the single record of that
validation, mirroring `docs/runbooks/PHASE-3-DOGFOOD.md`'s structure and
integrity rules exactly (the closest prior-phase precedent for a
dogfood-as-exit-gate document in this repository).

**This document records real usage. It does not simulate it.** No task,
review, or automated test may fill in a row on this operator's behalf. Each
row below is written only after a human operator has actually used the
Automation workspace (Workflows, Simulation, Policies, Approval inbox, Run
history, Kill switches) for a real workflow against real (if bounded, local)
side effects on that calendar day. Browser acceptance scenarios
(`frontend/e2e/scenarios/automation-lifecycle.mjs`, `automation-approvals-
keyboard.mjs`) and backend integration tests
(`tests/test_automation_*_postgres.py`) already prove the mechanisms work
against a mocked or fixture-seeded backend; this document proves the same
mechanisms hold up under a real human operator's own actual, unscripted
workspace use, which no automated test can substitute for.

## Status

**Open — 0 of 14 required days recorded.**

The gate below remains open until fourteen real, consecutive-or-near-daily
usage days are recorded by a human operator using the deployed application
under real conditions (a real `scripts/run_automation_worker.py` process
against a real PostgreSQL database, not a mocked fixture), each row filled
in honestly (including partial or negative days), and this status line is
updated to close the gate. This document being *created* with the correct
structure is not evidence of usage; it is the empty form the evidence goes
into.

## Judgment call: a 14-day window, split into two 7-day stages (reviewer should double-check)

`PHASE-005-automation.md` does not itself name an exact day count for this
phase's own dogfood window the way `PHASE-003-human-attention-engine.md`
named "two weeks (14 consecutive-or-near-daily days)" explicitly. Absent a
phase-specified number, this document picks **14 days total**, reasoned as
follows: (1) matching Phase 3's own already-approved precedent for "how long
is long enough to catch real, intermittent, human-workflow-shaped issues in
this repository" is a reasonable default absent a phase-specific reason to
diverge; (2) `PHASE-005-automation.md`'s own Exit criteria phrase --
"advances from preview to bounded actions" -- names a *staged* progression
this activation's own policy model (`automation_policies.approval_mode`:
`preview_only` -> `per_run`/`bounded_recurring`) already makes mechanically
real, which Phase 3's own flat 14-day window had no equivalent of. This
document therefore splits the 14 days into two distinct 7-day stages rather
than one undifferentiated window, so the record itself demonstrates the
staged progression the exit criterion names, not only the total duration:

- **Stage 1 -- Preview-only (days 1-7).** Every workflow's attached policy is
  `preview_only` (or the operator uses `/simulate` exclusively without ever
  starting a real run). Goal: prove simulation fidelity, the workflow
  builder, policy authoring and the UI's own simulation-vs-real visual
  distinction hold up under real, unscripted use -- with the success
  threshold of **zero real side effects of any kind** during this stage
  (mechanically guaranteed by `preview_only`'s own dispatch-gate semantics,
  `approvals.evaluate_approval_requirement`, and `/simulate`'s own
  structural inability to reach `execute()` -- this stage's threshold is
  therefore a real-world confirmation of an already-proven backend
  guarantee, not a new risk).
- **Stage 2 -- Bounded real actions (days 8-14).** At least one workflow's
  policy is switched to `per_run` or `bounded_recurring` and real runs are
  started against real local adapters (`local.create_note`, `local.
  send_test_notification`) under real approval gates, kill switches and
  (if triggered) compensation. Goal: prove the full authority lifecycle
  (approve, reject, pause, resume, cancel, revoke, kill switch) holds up
  under a real operator's own real decisions, not only the scripted
  fixture sequence `automation-lifecycle.mjs` already proves in a browser
  with a mocked backend.

A reviewer who considers 14 days too short (or too long) for this
activation's own real risk profile, or who would rather not split the
window into two fixed 7-day halves (e.g. preferring an operator-paced
transition once Stage 1's threshold is confidently met, rather than a fixed
calendar boundary), should treat this as the one judgment call in this
document most worth revisiting before the window opens for real.

## Approved success thresholds

Derived directly from `PHASE-005-automation.md`'s own Acceptance criteria
and Exit criteria (verbatim reference in parentheses), made concrete for a
human operator to actually score each day:

- **Zero unauthorized effects** (Exit criteria: "...with zero unauthorized
  effects") -- across the full 14-day window, no side effect (a real
  `local.create_note` write, a real `local.send_test_notification` call)
  ever occurs without either (a) an explicit, digest-matched human approval
  decision recorded in `approval_requests`, or (b) a `bounded_recurring`
  policy's own already-authorized, count-limited dispatch. Any occurrence
  where a real effect happened *without* one of these two authorizations is
  an automatic, non-negotiable gate failure requiring the repository
  owner's own explicit decision before the window can close (see "Closing
  the gate" below) -- not something a later day's row can quietly overwrite.
- **Zero simulation-caused side effects** (Acceptance criteria: "Simulation
  never causes side effects") -- every `/simulate` call during the window
  produces no real `notes`/`workflow_runs`/`workflow_run_steps` row change,
  confirmed by the operator directly (e.g. checking the Notes workspace or
  run history before and after a simulation) at least once during Stage 1.
- **Every approval decision correctly validated** (Acceptance criteria:
  "Unauthorized, expired, changed or replayed approvals are rejected") --
  during Stage 2, at least one deliberately-incorrect approval attempt
  (a wrong digest, an already-decided request, or an expired request) is
  attempted by the operator and confirmed rejected with the correct,
  specific error code (`digest_mismatch`/`approval_already_decided`/
  `approval_expired`), not merely a generic failure.
- **Pause/cancel/revoke/kill switches stop before the next side effect**
  (Acceptance criteria, verbatim) -- during Stage 2, at least one pause,
  one cancel, one policy revocation and one kill-switch activation are each
  exercised against a real, in-flight or queued run, and the operator
  confirms directly (via run detail) that no further step dispatched after
  the stop action.
- **Unknown outcomes and partial compensation are visible and recoverable**
  (Acceptance criteria, verbatim) -- if a `needs_review`/`unknown` step
  outcome or a `compensation_failed` run occurs at any point during the
  window (naturally, not manufactured), the operator confirms the UI
  surfaced it as a distinct, non-dismissible state (`UX-STATES.md`) and
  that they were able to resolve or recover it using only what the UI
  showed them, without reading source code or querying the database
  directly. If neither state occurs naturally during the window, the
  operator deliberately provokes at least one of them once during Stage 2
  (e.g. killing the worker process between steps to produce an `unknown`
  outcome) specifically to confirm this threshold, rather than leaving it
  unverified -- named explicitly in that day's row either way.

## Daily-use log

| Date | Operator | Stage (1 = preview-only, 2 = bounded real actions) | Workflows/policies exercised | Real side effects this day | Approval decisions (correct / deliberately-incorrect) | Pause/cancel/revoke/kill-switch exercised | Unknown outcome or partial compensation observed | Issues encountered | Resolution / follow-up |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |
| YYYY-MM-DD | | | | | | | | | |

Each row must be completed with:

- **Date**: the real calendar date (`YYYY-MM-DD`) the workspace was used, in the operator's local timezone.
- **Operator**: the person who used the application that day.
- **Stage**: `1` for days 1-7 (preview-only) or `2` for days 8-14 (bounded real actions) -- see the judgment-call section above for what each stage requires.
- **Workflows/policies exercised**: which workflow(s) and policy approval mode(s) were actually used that day, and for what real (even if small) task.
- **Real side effects this day**: every real side effect that actually occurred (e.g. "1 note created via local.create_note, run <id>"), or "none" -- never left blank.
- **Approval decisions**: how many correct approval/reject decisions were made, and (Stage 2 only) whether the deliberately-incorrect-attempt threshold was exercised that day (digest mismatch / already-decided / expired), or "n/a" for Stage 1.
- **Pause/cancel/revoke/kill-switch exercised**: which of these four stop mechanisms were exercised that day, against which run/workflow/policy, with the confirmed outcome (no further step dispatched), or "none" for a day none were exercised.
- **Unknown outcome or partial compensation observed**: "none" (an acceptable, honest entry for most days), or a description of what was observed and how the UI presented it, including whether it was deliberately provoked per the threshold above.
- **Issues encountered**: any bug, confusing state, missing capability, or friction actually hit that day -- "none" is an acceptable, honest entry, but the field must not be left blank.
- **Resolution / follow-up**: what was done about each issue (fixed immediately, filed as a follow-up task, accepted as a known Phase 5 limitation -- e.g. compensation dispatch not being crash-resumable, `docs/phases/phase-005/IMPLEMENTATION-STATUS.md`'s own disclosed Task 6 gap), or "n/a" if there were none.

## Closing the gate

The gate closes only when:

1. All fourteen rows above are filled in with real dates and non-empty content, with at least seven rows marked Stage 1 and at least seven marked Stage 2 (or a documented, explicit reason for a different split, decided by the repository owner, not silently improvised).
2. Every approved success threshold above is met when the log is aggregated across the full window (zero unauthorized effects; zero simulation-caused side effects; every approval-validation sub-case exercised and correctly rejected; every one of pause/cancel/revoke/kill-switch exercised at least once with a confirmed stop; the unknown-outcome/partial-compensation threshold satisfied either naturally or by deliberate provocation) -- or, if a threshold was missed, the shortfall is named explicitly and the repository owner has made an explicit decision (extend the dogfood window, ship with a documented known gap, or revise the policy/UX and restart the window) rather than the gate being silently closed anyway.
3. No row's "Issues encountered" entry represents an unresolved data-loss, security, workspace-isolation, unauthorized-side-effect, or prohibited-signal defect.
4. The "Status" line above is updated from "Open" to "Closed", naming the date range covered, the operator(s) involved, and the aggregated result against each threshold.
5. A human reviewer -- not the operator alone -- signs off in the change-review record referenced by the relevant pull request.

Until all five conditions hold, no other Phase 5 document may describe this
gate as satisfied, and no document may describe Phase 5 itself as complete,
done, or shipped.
