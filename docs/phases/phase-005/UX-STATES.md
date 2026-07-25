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

Destructive/high-impact approvals (`APPROVAL-POLICY.md`'s taxonomy) require deliberate confirmation: the exact target, payload summary, high-impact category, reversible status and expiry are shown before the approve action is reachable, and the approval UI never pre-fills or auto-submits a decision. Simulation results (`API-SCHEMAS.md`'s `/simulate`) are visually distinct from real run history at every surface -- a simulated run can never be mistaken for one that produced real side effects, matching the acceptance criterion "simulation never causes side effects" being visible, not just true.

All flows are keyboard accessible and meet WCAG 2.2 AA.
