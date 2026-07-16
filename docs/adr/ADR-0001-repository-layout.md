---
id: ADR-0001
title: Repository Layout
status: Accepted
date: 2026-07-13
owners:
  - Lucky Jain
related:
  - STD-001
  - PHASE-000
---

# ADR-0001 — Repository Layout

## Context

ECC requires a structure that separates normative specifications, architecture decisions, implementation code and tests while remaining easy for humans and AI coding agents to navigate.

## Decision

Use a monorepo with these top-level areas when implementation begins:

```text
docs/
backend/
frontend/
packages/
tests/
scripts/
infrastructure/
.github/
```

`docs/` remains the source of truth for RFCs, standards, ADRs and phase specifications. Domain code is grouped by business capability, not by framework layer alone.

No new top-level directory may be introduced without an ADR.

## Consequences

- One repository contains the full product context.
- Cross-cutting changes are easier to review atomically.
- CI must support path-aware checks.
- Repository size must be actively managed.

## Alternatives considered

- Multi-repository architecture: rejected for Phase 0 because it fragments context and increases coordination overhead.
- Framework-first folders: rejected because they weaken domain ownership.
