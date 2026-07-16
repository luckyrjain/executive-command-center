---
id: PHASE-008-DELEGATION
title: Delegation Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Delegation Contract

Delegation identifies delegator, recipient, obligation/resource, expected outcome, due time, shared evidence and allowed actions. States: `proposed -> accepted|rejected|expired`; accepted becomes `completed|revoked|cancelled`.

Accountability transfers only on acceptance. The original history remains visible. Revocation does not erase actions already taken. Reassignment creates a new proposal. Notifications are idempotent and respect preferences. A recipient never gains access beyond evidence explicitly required for the delegation.
