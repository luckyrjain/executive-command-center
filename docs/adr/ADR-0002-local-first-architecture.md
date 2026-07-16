---
id: ADR-0002
title: Local-First Architecture
status: Accepted
date: 2026-07-13
owners:
  - Lucky Jain
related:
  - RFC-001
  - RFC-004
---

# ADR-0002 — Local-First Architecture

## Context

ECC handles sensitive executive information and must remain useful during temporary internet or cloud-service outages.

## Decision

Core workflows, primary data storage, search, scheduling and the default AI path SHALL operate locally. Cloud services are optional adapters and may enhance capability but may not become mandatory for core operation.

The local installation is authoritative for user-owned data. Synchronization must be explicit, auditable and reversible.

## Consequences

- Privacy, resilience and latency improve.
- Backup, encryption and migration become first-class local concerns.
- Some cloud-only features will degrade gracefully when unavailable.

## Alternatives considered

- SaaS-first architecture: rejected because it conflicts with product principles.
- Offline cache backed by cloud authority: rejected because local operation would remain subordinate to connectivity.
