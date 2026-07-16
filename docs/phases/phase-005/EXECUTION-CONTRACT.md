---
id: PHASE-005-EXECUTION
title: Durable Execution Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Durable Execution Contract

Workflow graphs are finite and versioned. The worker persists state before and after each side effect. A stable action digest and idempotency key prevent duplicate execution. Restart resumes from the last durable checkpoint.

Retries use bounded exponential backoff only for classified transient failures. Unknown external outcomes move to `needs_review`; they are not blindly retried. Parallel branches have explicit join semantics. Cancellation stops before the next side effect. Compensation executes only declared, approved steps and records partial recovery.
