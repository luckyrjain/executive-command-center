---
id: ADR-0004
title: AI Runtime
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
related: [RFC-004, RFC-005]
---

# ADR-0004 — AI Runtime

## Context
ECC requires summarization, extraction, planning and recommendation while preserving explainability and human authority.

## Decision
All model calls pass through a single AI Runtime composed of a Model Router, prompt registry, structured-output validation, tool permission checks, evaluation hooks and audit logging. Domain services never call model providers directly. AI outputs are proposals until validated and accepted by deterministic domain logic or a human.

## Consequences
- Provider replacement and local/cloud routing remain centralized.
- Prompt and model versions become auditable.
- Runtime availability does not determine source-of-truth state.
- Additional latency is accepted for validation and traceability.

## Alternatives considered
Direct model calls from each feature were rejected because they fragment policy, evaluation and observability.
