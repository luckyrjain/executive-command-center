---
id: PHASE-004-MODEL-ROUTING
title: Model Routing Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Model Routing Contract

Routing is deterministic for a task, policy version, availability snapshot and data class. Eligibility is evaluated before quality/cost preference: data residency and privacy, required capability, structured-output support, context limit, health, latency and budget. Local models are preferred when they meet the approved quality floor. Remote use requires explicit configuration.

Retries are bounded and never repeat a non-idempotent tool. Fallback changes are recorded. Circuit breakers exclude unhealthy deployments. A caller cannot override policy or enable an unapproved provider. Promotion of a model/policy requires evaluation evidence and rollback pointer.
