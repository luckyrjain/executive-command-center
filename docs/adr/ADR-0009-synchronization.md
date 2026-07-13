---
id: ADR-0009
title: Connector Synchronization
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
related: [RFC-004, EVENT-CATALOG]
---

# ADR-0009 — Connector Synchronization

## Context
External systems remain authoritative for email, calendar, source control and work tracking while ECC needs local searchable projections.

## Decision
Connectors use incremental, cursor-based synchronization where supported. Every imported record stores source system, source identifier, source revision, observed timestamp and synchronization cursor. Imports are idempotent. Source artifacts are preserved before normalization.

ECC may enrich imported data locally but must not overwrite the external source without an explicit user-approved command. Conflicts are recorded and surfaced; they are never resolved silently.

## Consequences
- Re-sync and replay are safe.
- Provenance is available for every imported fact.
- Connector-specific cursor expiry and deletion semantics require tests.
- Two-way synchronization is deferred per connector and requires explicit approval flows.

## Alternatives considered
Full replacement syncs were rejected because they are costly, fragile and poor at preserving deletion history.
