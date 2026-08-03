---
id: PHASE-008-DELEGATION
title: Delegation Contract
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
---

# Delegation Contract

Delegation identifies delegator, recipient, obligation/resource, expected outcome, due time, shared evidence and allowed actions. States: `proposed -> accepted|rejected|expired|cancelled`; accepted becomes `completed|revoked|cancelled`. The `proposed -> cancelled` edge is system-initiated only (either party being removed from the workspace force-cancels a still-pending delegation naming them, `ecc.domains.collaboration.delegations.cancel_delegations_for_removed_member`) -- there is no user-initiated way for a delegator to withdraw a still-`proposed` delegation themselves; only wait for the recipient to accept/reject, or for it to expire.

Accountability transfers only on acceptance. The original history remains visible (`delegation_events`, append-only). Revocation does not erase actions already taken. Reassignment creates a new proposal. Notifications are idempotent and respect preferences. A recipient never gains access beyond evidence explicitly required for the delegation -- acceptance creates scoped, read-only `resource_grants` rows (the same mechanism `PERMISSION-CONTRACT.md` defines generally) naming exactly the evidence the delegation itself names, never a broader grant, auto-revoked the moment the delegation reaches any terminal state (`completed`/`revoked`/`cancelled`).
