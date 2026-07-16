---
id: ADR-0007
title: Model Router
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
related: [ADR-0004, RFC-005]
---

# ADR-0007 — Model Router

## Context
Different tasks require different models, privacy levels, latency and cost profiles.

## Decision
Introduce a provider-neutral Model Router as the only entry point for inference. Routing is policy based using task type, data sensitivity, required capability, context size, latency budget and configured provider availability. Local models are preferred for sensitive content. Fallbacks must be explicit and auditable; sensitive requests may not silently fall back to cloud providers.

## Consequences
- Model selection is centralized and testable.
- Features depend on capabilities, not vendor APIs.
- Routing policy and fallback behaviour require versioning and evaluation.

## Alternatives considered
Feature-owned model selection was rejected because it duplicates policy and increases privacy risk.
