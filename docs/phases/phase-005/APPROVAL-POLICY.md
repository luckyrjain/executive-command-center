---
id: PHASE-005-APPROVAL-POLICY
title: Automation Approval Policy
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Automation Approval Policy

Authority is least-privilege, explicit, time-bound and revocable. A policy identifies workflow/version, action types, connector targets, data classes, value/count/rate limits, schedule, approval mode and expiry.

Modes are `preview_only|per_run|bounded_recurring`. Destructive, financial, legal, security, credential, public-posting and person-directed actions always require per-run approval unless a later security RFC explicitly permits otherwise. Approval displays exact target, payload summary, risk, reversible status and expiry. Material changes invalidate approval.
