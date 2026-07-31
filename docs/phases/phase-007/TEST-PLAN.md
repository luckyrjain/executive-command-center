---
id: PHASE-007-TEST-PLAN
title: Phase 7 Test Plan
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 7 Test Plan

Test domain opt-in/out, records, goals, routines, consent scopes/expiry/revocation, cross-domain denial, insight evidence, export and deletion propagation. Adversarial cases cover diagnosis, guaranteed returns, sensitive inference, coercive wording and prompt injection.

Verify encryption, local-only policy, remote egress denial, audit redaction, backups and workspace isolation. Browser acceptance enables a domain, captures data, grants/revokes access, inspects/dismisses an insight, exports and deletes.

## Task 1 status

Covers: `habits` domain opt-in/out, disabled-domain-blocks-all-access, cross-workspace/cross-user isolation, encrypted-field-never-returned-in-list-view, export completeness and deletion propagation, and the always-in-scope adversarial fixtures (no diagnostic/scoring language leaking even from a `standard` domain). Cross-domain denial, remote-egress denial and the `health`/`finance` adversarial rubric are exercised by the tasks that introduce a second domain, an AI-generated insight, and the `high_stakes` tier respectively -- not yet reachable in Task 1's own scope (zero AI calls, one domain).
