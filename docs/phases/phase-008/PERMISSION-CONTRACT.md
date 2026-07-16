---
id: PHASE-008-PERMISSIONS
title: Multi-user Permission Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Multi-user Permission Contract

Authorization evaluates active membership, role, resource visibility, explicit grant/deny, ownership, action and time. Deny and privacy constraints override role grants. Workspace administrators cannot read personal/private vaults merely because they administer membership.

Checks occur in service and query boundaries; UI hiding is not security. Background jobs snapshot no broader authority than the initiating policy and re-check before side effects. Permission changes invalidate sessions/caches promptly. Authorization decisions have redacted audit evidence and a versioned policy.
