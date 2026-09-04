---
id: SCR-0001
title: Reconcile RFC-004 architecture chapters and ADR-0005 with the as-built system
status: Approved
requester: Matt architecture review (2026-09-04)
date: 2026-09-04
affected_documents:
  - docs/architecture/chapter-02a-core-platform.md
  - docs/architecture/chapter-08-data-platform.md
  - docs/architecture/chapter-09-security.md
  - docs/adr/ADR-0005-event-bus.md
---

# Specification Change Request — Reconcile RFC-004 architecture chapters and ADR-0005 with the as-built system

## Ambiguity or conflict

An exhaustive architecture review (2026-09-04) compared `docs/architecture/chapter-02a-core-platform.md`,
`chapter-08-data-platform.md`, and `chapter-09-security.md` against the implementation in `backend/ecc/`.
Per `docs/00-document-control.md`'s golden rule ("if it is not documented in the current approved phase, it
does not get implemented") and spec-code synchronization rule ("behavior-changing code changes update the
governing specification in the same pull request"), these chapters are supposed to be the governing,
current spec — but several of their central claims describe subsystems that were never built, with no
Specification Change Request ever filed for the deviation:

- **Application Gateway** (chapter-02a: "the only public backend interface... every client request enters
  here," owning auth/authz/rate limiting/session management/API aggregation) does not exist.
  `backend/ecc/main.py` mounts ~50 domain routers directly on the FastAPI app; auth/authz are enforced via
  a shared dependency (`auth.py`/`authz.py`) imported per-router, not a separate gateway service.
- **Memory Engine** (chapter-02a: working/long-term/semantic/episodic memory, retrieval, indexing, ranking)
  has no corresponding module, table, or class anywhere in `backend/ecc`.
- **Polyglot persistence via PKOS** (chapter-08: Neo4j for relationships, Qdrant for vectors, Redis for
  cache/sessions/queues/rate limits, "business domains never communicate directly with Neo4j, PostgreSQL,
  Qdrant or Redis... they communicate with PKOS") does not exist. The stack is Postgres + pgvector only
  (`docker-compose.yml`, `pyproject.toml`). "PKOS" in code names two Postgres tables (`pkos_nodes`,
  `pkos_evidence`), not an abstraction layer, and the chapter's fitness functions (no business domain
  imports a database client directly; every query executes through PKOS) are violated by the domain layer
  broadly — 73 of ~104 non-test files under `backend/ecc/domains/` import `sqlalchemy.text` and execute
  hand-written SQL directly inside route handlers.
- **ADR-0005 (Event Bus)** is `status: Accepted` and its Decision names "Direct service-to-service calls for
  all workflows were rejected because they create synchronous coupling and cascading failure" as the
  rejected alternative. The implementation does the opposite: `backend/ecc/platform/events/bus.py`'s
  in-process bus is defined but never instantiated or called from any production code path; the durable
  `event_outbox` table (`platform/audit_outbox.py`) is written to by several domains but never read from
  outside its own writer, making it an audit log rather than a message bus; and cross-domain writes
  (e.g. `domains/governance/recommendation_targets.py` calling into `communication`, `planning`, and its
  own `governance` mutation functions directly) use exactly the synchronous, direct-call pattern the ADR
  says was rejected.
- **chapter-09-security.md**'s five-tier PII classification (Public/Internal/Confidential/Sensitive/
  Restricted) with automatic detection and masking does not exist. What exists is a narrower, different
  mechanism: a three-tier `Classification` (`standard`/`sensitive`/`high_stakes`) scoped only to the six
  Phase-7 personal-domain `domain_key`s, driving field-level encryption, not the doc's search/sharing/
  AI-access/export gating.

This is not scattered drift on minor points — it is the central architectural narrative of two full
chapters, plus one Accepted ADR's core decision, describing a system that does not exist, with the actual
system's real design (a single-database FastAPI monolith with per-router auth and no messaging layer)
undocumented anywhere as a deliberate decision.

## Current specification

`chapter-02a-core-platform.md` and `chapter-08-data-platform.md` are `status: Draft` RFC-004 chapters (never
promoted to Approved) that describe an Application Gateway, a Memory Engine, and Neo4j/Qdrant/Redis polyglot
persistence behind a PKOS abstraction as load-bearing, current architecture. `ADR-0005-event-bus.md` is
`status: Accepted` and describes a durable, consumed event bus as the cross-domain communication mechanism,
with a Phase 0 allowance for "an in-process durable implementation behind an event-bus contract."

## Proposed change

Add an **Implementation Status** section to the top of `chapter-02a-core-platform.md` and
`chapter-08-data-platform.md`, immediately after the Executive Summary, stating plainly which described
subsystems are built, which are not, and what the actual current mechanism is for each — without deleting
or rewriting the original vision content, since these chapters remain useful as the target/aspirational
design this review found no evidence the project has abandoned. Add an equivalent short note to
`chapter-09-security.md`'s Privacy Model section. Add an **Implementation Note** to `ADR-0005` documenting
that even the Phase 0 fallback (an in-process durable implementation) was not built as a consumed bus —
what was built is a write-only audit-outbox table plus direct synchronous cross-domain calls — and that no
superseding ADR has been filed for this deviation.

## Reason

An agent or engineer using these chapters as ground truth (as instructed by `docs/CONTRIBUTING.md` and this
review's own brief) will look for a Gateway module, a Memory service, and Neo4j/Qdrant clients that do not
exist, and will believe PII is auto-classified and masked when it is not. Per this repo's own golden rule,
undocumented deviation from the governing spec is itself the defect being corrected here — not a request to
change behavior, but to bring the documentation into compliance with the spec-code synchronization rule
after the fact.

## Product impact

None — documentation-only change, no behavior changes.

## Architecture impact

None — documents the architecture as it already is; does not propose adopting the Gateway/Memory Engine/
polyglot-persistence/event-bus design, nor does it propose deleting those design sections. A future SCR
should decide, explicitly, whether ECC still intends to build toward the original RFC-004 vision or whether
these chapters should be re-scoped as historical design exploration.

## Security and privacy impact

None on the system itself. Corrects a misleading claim (automatic PII detection/masking) that could
otherwise cause a future reviewer or auditor to believe a control exists that doesn't.

## Migration impact

None.

## Alternatives considered

- **Rewrite the chapters entirely to describe only the as-built system.** Rejected: would destroy the
  chapters' value as a record of original intent, and this review found no evidence the Gateway/Memory
  Engine/polyglot-persistence direction was deliberately abandoned versus simply not yet reached — that is
  a product decision outside this review's scope, not something to resolve unilaterally by deleting text.
- **Do nothing, rely on `docs/agents/domain.md`'s "if it doesn't exist, proceed silently" guidance.** Rejected:
  that guidance covers `CONTEXT.md`/ADRs that haven't been created yet, not existing, Approved-adjacent
  chapters actively describing non-existent subsystems as current fact.

## Approval

Self-approved as a documentation-only correction under the architecture review's standing mandate; no
behavior change, no migration, no security impact.

## Resulting document updates

- `docs/architecture/chapter-02a-core-platform.md` — Implementation Status section added.
- `docs/architecture/chapter-08-data-platform.md` — Implementation Status section added.
- `docs/architecture/chapter-09-security.md` — Implementation Status note added to Privacy Model section.
- `docs/adr/ADR-0005-event-bus.md` — Implementation Note added under Consequences.
