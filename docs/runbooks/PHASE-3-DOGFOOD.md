# Phase 3 Dogfood Validation Record

## Purpose

`docs/phases/PHASE-003-human-attention-engine.md`'s Exit criteria and
`docs/phases/phase-003/IMPLEMENTATION-STATUS.md` both name a two-week
(14 consecutive-or-near-daily) dogfood validation as a required exit item,
separate from and in addition to every automated CI/acceptance gate. This
document is the single record of that validation, mirroring
`docs/runbooks/PHASE-1-DAILY-USE.md`'s structure and integrity rules.

**This document records real usage. It does not simulate it.** No task,
review, or automated test may fill in a row on this operator's behalf.
Each row below is written only after a human operator has actually used
the Attention Queue, Waiting, Risk Review Queue, Planner, or Meeting Prep
surfaces for real executive-workspace decisions on that calendar day.

## Status

**Open — 0 of 14 required days recorded.**

The gate below remains open until fourteen real, consecutive-or-near-daily
usage days are recorded by a human operator using the deployed application
under real conditions, each row filled in honestly (including partial or
negative days), and this status line is updated to close the gate. This
document being *created* with the correct structure is not evidence of
usage; it is the empty form the evidence goes into.

## Approved success thresholds

Per `docs/phases/PHASE-003-human-attention-engine.md`'s "Dogfood success
thresholds (approved 2026-07-23)":

- **Zero missed critical items** across the two-week window (critical =
  overdue, due/reviewable within 48 hours, or blocking a dependent item,
  per `docs/phases/phase-003/ATTENTION-MODEL.md`'s critical-item
  definition).
- **≥80% top-five usefulness rating** (of the attention queue's top five
  items on a given day, the fraction the operator judged actually useful).
- **Plan acceptance rate ≥60%** (below that, the planner is proposing
  infeasible plans).
- **False-urgency rate <10%** (items surfaced as urgent that turned out not
  to be).

## Daily-use log

| Date | Operator | Primary surfaces exercised | Top-five usefulness | Missed critical items | False urgency | Plan proposed/accepted | Meeting-pack corrections | Issues encountered | Resolution / follow-up |
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
- **Primary surfaces exercised**: which of Attention Queue, Waiting, Risk Review Queue, Planner, or Meeting Prep were actually used, and for what real decision or action.
- **Top-five usefulness**: of the day's top five attention-queue items, how many the operator judged actually useful (e.g. "4/5"), or "n/a" if the surface wasn't used that day.
- **Missed critical items**: any item that should have surfaced as critical (per the definition above) but didn't, named specifically — "none" is an acceptable, honest entry.
- **False urgency**: any item surfaced as urgent that the operator judged, in hindsight, was not — named specifically, or "none".
- **Plan proposed/accepted**: whether a plan was proposed that day and, if so, whether it was accepted, replanned, or superseded without acceptance (e.g. "proposed, accepted" / "proposed, replanned then accepted" / "not used").
- **Meeting-pack corrections**: any factual error, missing citation, or wrong evidence-gap the operator had to correct after reading a generated meeting-prep pack — "none" if none, "n/a" if meeting prep wasn't used.
- **Issues encountered**: any bug, confusing state, missing capability, or friction actually hit that day — "none" is an acceptable, honest entry, but the field must not be left blank.
- **Resolution / follow-up**: what was done about each issue (fixed immediately, filed as a follow-up task, accepted as a known Phase 3 limitation), or "n/a" if there were none.

## Closing the gate

The gate closes only when:

1. All fourteen rows above are filled in with real dates and non-empty content.
2. The approved success thresholds above are met when the log is aggregated across the full window (zero missed critical items over the whole window; top-five usefulness ≥80% on average; plan acceptance rate ≥60%; false-urgency rate <10%) — or, if a threshold was missed, the shortfall is named explicitly and the repository owner has made an explicit decision (extend the dogfood window, ship with a documented known gap, or revise the policy/UX and restart the window) rather than the gate being silently closed anyway.
3. No row's "Issues encountered" entry represents an unresolved data-loss, security, workspace-isolation, or prohibited-signal defect.
4. The "Status" line above is updated from "Open" to "Closed", naming the date range covered, the operator(s) involved, and the aggregated metrics against each threshold.
5. A human reviewer — not the operator alone — signs off in the change-review record referenced by the relevant pull request.

Until all five conditions hold, no other Phase 3 document may describe this
gate as satisfied, and no document may describe Phase 3 itself as complete,
done, or shipped.
