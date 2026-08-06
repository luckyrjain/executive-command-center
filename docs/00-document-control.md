# 00 — Document Control

## Document metadata

- **Document name:** Executive Command Center Engineering Specification
- **Status:** Approved
- **Version:** 1.2
- **Owner:** Lucky Jain
- **Repository:** `luckyrjain/executive-command-center`
- **Specification root:** `docs/`
- **Last updated:** 2026-08-06

## Purpose

This repository is the single source of truth for the Executive Command
Center specification. Current phase state is machine-readable in
[`docs/phases/status.json`](phases/status.json); README, roadmap, and phase
index status summaries are generated from it.

## Golden rule

If it is not documented in the current approved phase, it does not get
implemented.

## Specification change process

1. Identify the gap or change.
2. Create a specification change request when behavior or a normative
   contract changes.
3. Update affected RFCs, ADRs, standards, phase contracts, and acceptance
   criteria.
4. Update the canonical phase registry when current phase state changes.
5. Run `make docs-check`.
6. Only then update implementation behavior.

## Spec-code synchronization

Behavior-changing code changes update the governing specification in the
same pull request. Documentation-only status changes may not claim a gate is
closed without durable evidence as defined in
[`docs/evidence/README.md`](evidence/README.md).

## Document inheritance

Phase documents inherit from this maintained specification set:

- [SPEC-000 — Constitution](specifications/SPEC-000.md)
- [RFC-000 — Specification Governance](RFC-000.md)
- [RFC-001 — Product Definition](RFC-001.md)
- [RFC-002 — Engineering Philosophy](RFC-002.md)
- [RFC-003 — Design Principles](RFC-003.md)
- [RFC-004 — System Architecture](RFC-004.md) and its
  [architecture chapters](architecture/)
- [RFC-005 — Approved Technology Registry](RFC-005.md)
- [STD-001 — Repository Standards](standards/STD-001.md)
- [Canonical domain contracts](domain/)
- [Phase specifications and contracts](phases/)
- [Security baseline](security/PHASE-0-SECURITY-BASELINE.md)
- [Operations records](operations/) and [runbooks](runbooks/)

## Stop-and-ask protocol

When the specification is ambiguous, contradictory, or incomplete, the
implementation agent stops and requests a specification decision instead of
guessing.
