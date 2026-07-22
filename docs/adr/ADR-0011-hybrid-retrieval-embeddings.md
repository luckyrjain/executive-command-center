---
id: ADR-0011
title: Hybrid Retrieval Embeddings
status: Accepted
date: 2026-07-22
owners:
  - Lucky Jain
related:
  - RFC-005
  - ADR-0003
  - ADR-0006
  - PHASE-002-RETRIEVAL
---

# ADR-0011 — Hybrid Retrieval Embeddings

## Context

`RETRIEVAL-CONTRACT.md` (Approved for Implementation since Phase 2's contracts moved out of Draft) already specifies a `hybrid` retrieval mode and a semantic-relevance rank below lexical relevance, but Slice 6 shipped lexical-only retrieval and left `mode=hybrid` permanently degraded, since embeddings require a new technology addition RFC-005 gates behind "Retrieval benchmark and ADR" (`RFC-005.md`'s "Approved later" table). The Phase 2 design doc's Open decision 2 named the same gate and explicitly deferred both the benchmark and this ADR to whenever the repository owner decided embeddings were worth pursuing, given that Slice 6 alone already satisfies `chapter-04-knowledge-platform.md`'s "if embeddings fail, graph traversal continues" fallback-first principle.

The repository owner has now authorized proceeding (see `docs/ROADMAP.md`'s Phase 2 status note for how this authorization is recorded, mirroring the precedent set for Phase 2's own parallel-start authorization). This ADR is the other half of RFC-005's activation gate alongside the RFC-005 v1.2.0 amendment.

## Decision

Activate `pgvector` (Postgres vector extension, via the `pgvector/pgvector:pg18` image) as the vector storage/ANN-search layer, and add `sentence-transformers` (local, CPU-only inference; model `sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions) as the embedding-generation library — both local-first, no external network calls at query or embed time, consistent with ADR-0002's local-first architecture decision.

Embeddings are generated from the same `retrieval_documents` projection Slice 6 already maintains (title + current claim content), stored in a new `embedding_projections` table keyed by `(workspace_id, document_id, model_id)`, and are a derived, replaceable projection per ADR-0003 — rebuildable from `retrieval_documents` at any time (`scripts/rebuild_knowledge_projections.py`), never a second source of truth. Generation and lookup are both best-effort: `Settings.embeddings_enabled` (default off) and any failure of the model to load or run degrade to lexical-only with `degraded=true`, never a failed request — extending, not replacing, `RETRIEVAL-CONTRACT.md`'s existing degradation rule to cover the write path (`queue_embedding`) as well as the read path (`GET /knowledge/retrieve?mode=hybrid`).

Hybrid ranking fuses lexical and semantic candidates with a versioned deterministic formula (`backend/ecc/domains/knowledge/retrieval.py`'s `_SCORE_*` constants): a document already found lexically gets a small, capped semantic bonus; a document found only semantically is scored from similarity alone, capped strictly below the lowest lexical-relevance band. This preserves `RETRIEVAL-CONTRACT.md`'s existing ranking order (exact identifier > exact name > exact alias > lexical relevance > semantic relevance) exactly — semantic similarity never promotes a result above what lexical relevance alone would achieve, it only adds recall for documents lexical search cannot reach and a small tie-break boost when both signals agree.

## Consequences

### Positive

- Executive search can now surface conceptually related entities (a differently-worded but semantically matching claim or title) that lexical trigram/full-text search structurally cannot find.
- No new operational service: embeddings run in-process against the same PostgreSQL instance; no vector database, no external embedding API, no new network egress.
- Fully optional and reversible: `Settings.embeddings_enabled=false` (the default) makes every code path introduced here a no-op, and `embedding_projections` can be dropped and rebuilt without touching any authoritative table.

### Negative

- `sentence-transformers` pulls in `torch` as a transitive dependency — a multi-hundred-MB addition to the backend's dependency footprint and container image size, the largest single dependency this codebase has added to date. Accepted as the cost of genuine local-first inference (the alternative, an external embedding API, was rejected below).
- First-call model load costs a multi-second delay and (until cached) a Hugging Face Hub download; mitigated by lazy, once-per-process loading and by keeping the feature off by default.
- A second `model_id` requires an explicit `rebuild_embeddings` run to bring existing workspaces onto it — not automatic, by design (mirrors ADR-0003's "derived, replaceable projections" framing: a projection is rebuilt deliberately, not silently migrated in place).

### Risks

- CPU-only inference at scale (many entities embedded in a short window) could become a measurable latency cost on the mutation path; `queue_embedding`'s content-hash skip (only re-embed on genuine title/body change) bounds this to actual content changes, not every mutation.
- A poorly-tuned fusion formula could either bury genuinely relevant semantic-only results (bonus/ceiling too low) or let semantic noise crowd out precise lexical matches (ceiling too high); the formula's constants are explicitly versioned in `retrieval.py` and any change requires a before/after benchmark per `RETRIEVAL-CONTRACT.md`'s Evaluation section, matching the same discipline already required for lexical ranking changes.

## Alternatives considered

- **External embedding API** (e.g. a hosted embeddings endpoint): rejected. Contradicts ADR-0002's local-first principle and RFC-005's Phase 0 exclusion of cloud-only dependencies — every entity/claim's text content would leave the local Postgres boundary on every embed, and per-call cost/latency would depend on a third-party network dependency this repository has otherwise avoided everywhere else in the retrieval and knowledge domains.
- **Qdrant** (dedicated vector database, also gated in RFC-005's "Approved later" table): rejected for this activation. Introduces a second operational database Phase 0/1/2 have consistently avoided (ADR-0006's "one operational database" consequence); pgvector inside the existing PostgreSQL instance satisfies the same retrieval need without a new service to deploy, back up, or restore separately from the rest of the schema.
- **Do nothing (leave Slice 7 permanently unscheduled)**: the design doc's own Open decision 2 raised this as a live possibility, since Slice 6 alone satisfies the fallback-first principle. Rejected now that the repository owner has explicitly authorized proceeding; recorded here rather than silently declined so the reasoning for either choice stays visible.
