---
id: DESIGN-2026-08-06-DOCUMENTATION-GOVERNANCE-REPAIR
title: Documentation Governance Repair
status: Approved
version: 1.0.0
owner: Lucky Jain
reviewers:
  - Lucky Jain
created: 2026-08-06
updated: 2026-08-06
type: Design Specification
depends_on:
  - RFC-000
  - STD-001
---

# Documentation Governance Repair

## Decision

Repair the documentation control plane before adding another major phase. The repair introduces one machine-readable phase-status source, automated documentation validation, generated status summaries, corrected current documentation, the missing Phase 10 contract set, and explicit operational-readiness records.

This change does not implement new runtime authentication, recovery, encryption-key rotation, product telemetry, connector, or Gmail behavior. Where the current system lacks an operational capability, the documentation records a release blocker and an accountable follow-up instead of describing the capability as available.

## Problem

ECC declares its specification authoritative, but phase status and release claims are duplicated across the README, roadmap, phase index, historical phase review, phase specifications, implementation-status reports, runbooks, and setup guide. Those copies have diverged.

The current repository also has governance rules that are not mechanically enforced:

- normative documents do not use a consistent metadata/status vocabulary;
- status narratives are stored in metadata fields intended for bounded state values;
- required changelog and specification-freeze rules are not applied consistently;
- the Phase 0-9 review is presented as active even though it is a historical snapshot;
- Phase 10 has implementation evidence but not the normal supporting contract set;
- `.env.example` does not cover every runtime setting;
- Phase 1 documents cite review files that do not exist in the repository;
- production-readiness gaps are spread across phase documents instead of being visible in one gate;
- CI does not detect documentation drift.

## Goals

1. Make current phase state answerable from one source.
2. Prevent status, metadata, link, configuration, evidence, and phase-contract drift in CI.
3. Bring README, roadmap, setup, indexes, governance documents, and Phase 10 contracts in line with current `main`.
4. Distinguish engineering completion from product validation, production readiness, and promotion.
5. Make unsupported production operations explicit blockers.
6. Keep the validator dependency-free and runnable with the repository's supported Python runtime.

## Non-goals

- Implementing first-production-owner provisioning.
- Implementing password reset, account recovery, MFA, passkeys, or step-up authentication.
- Implementing encryption-key rotation or re-encryption jobs.
- Implementing product analytics or telemetry collection.
- Filling human dogfood logs without real operator usage.
- Changing Phase 10 runtime scope or implementation.
- Rewriting historical implementation journals.
- Introducing a documentation site generator or a new third-party dependency.

## Canonical phase registry

Create `docs/phases/status.json` with schema version `1`. It is the only source for current phase state.

Each phase entry contains:

```json
{
  "id": "PHASE-010",
  "number": 10,
  "name": "Gmail Connector",
  "specification": "docs/phases/PHASE-010-gmail-connector.md",
  "specification_status": "approved_for_implementation",
  "engineering_status": "in_progress",
  "engineering_summary": "Tasks 1-2 of 8 complete",
  "validation_status": "not_started",
  "promotion_status": "not_promoted",
  "open_gates": [
    "Tasks 3-8",
    "real Gmail account verification",
    "backup and restore verification",
    "independent change review"
  ],
  "implementation_status": "docs/phases/phase-010/IMPLEMENTATION-STATUS.md",
  "last_verified_commit": "0919ca4fd7a5fbd6c2199606a61652cdc3ff9ade"
}
```

Allowed phase state values are closed sets:

- `specification_status`: `draft`, `review`, `approved_for_implementation`, `implemented`, `deprecated`, `archived`
- `engineering_status`: `not_started`, `in_progress`, `engineering_complete`, `not_applicable`
- `validation_status`: `not_started`, `in_progress`, `passed`, `failed`, `accepted_with_limitations`, `not_applicable`
- `promotion_status`: `not_promoted`, `promoted`, `blocked`, `not_applicable`

An approved specification does not imply engineering completion. Engineering completion does not imply product validation or production promotion. A phase with open predecessor gates may proceed only when its registry entry names the exception; that exception never changes the predecessor's state.

## Generated status surfaces

Create `scripts/docs_status.py` with two commands:

- `python scripts/docs_status.py render` updates generated status blocks.
- `python scripts/docs_status.py check` exits non-zero when committed blocks differ from registry output.

Generated blocks use stable comments:

```markdown
<!-- BEGIN GENERATED PHASE STATUS -->
...
<!-- END GENERATED PHASE STATUS -->
```

The generator owns only those blocks in:

- `README.md`
- `docs/ROADMAP.md`
- `docs/phases/README.md`

Narrative product and architecture content remains hand-authored. Generated content includes the current phase, a compact phase table, open validation gates, and a link to the canonical registry.

`docs/phases/PHASE-REVIEW.md` becomes an archived Phase 0-9 snapshot with an explicit `as_of` date and a banner linking to the current registry. Its historical conclusions are not rewritten as if they were current.

## Documentation validator

Create `scripts/check_docs.py`, implemented with the Python standard library, plus `tests/test_check_docs.py`.

The validator checks:

1. `status.json` schema, unique phase numbers/IDs, closed state values, existing referenced files, and full phase coverage.
2. Generated status blocks match `docs_status.py` output.
3. Relative Markdown links and local heading anchors resolve outside fenced code blocks.
4. Normative documents have required metadata: `id`, `title`, `status`, `version`, `owner`.
5. Status values match the lifecycle for the document type.
6. Every phase specification is represented in the registry and phase index.
7. Approved phases contain every contract named in their `contracts` metadata.
8. No documentation cites `.superpowers/sdd/` as inspectable evidence.
9. Every `Settings.validation_alias` in `backend/ecc/config.py` is represented in `.env.example`, except aliases explicitly listed in a small documented exclusion set for non-configuration process controls.
10. The legacy filenames removed from `docs/00-document-control.md` do not reappear as authoritative inheritance targets.

The validator does not make semantic claims about whether implementation matches a contract. It detects structural drift only.

Add the validator and generated-status check to the backend CI job before dependency installation where possible, because both are standard-library-only. Add `make docs-check` and document the command in contribution/setup guidance.

## Metadata and status lifecycle

Update RFC-000 so its rules match the repository's actual document types.

Required lifecycle values:

- RFC/standard/specification: `Draft`, `Review`, `Approved`, `Implemented`, `Deprecated`, `Archived`
- ADR: `Proposed`, `Accepted`, `Implemented`, `Superseded`, `Archived`
- phase specification/contract: `Draft`, `Review`, `Approved for Implementation`, `Implemented`, `Deprecated`, `Archived`
- implementation status: `Planned`, `Active`, `Closed`, `Archived`
- runbook/validation record: `Draft`, `Active`, `Open`, `Closed`, `Archived`
- planning/design artifact: `Draft`, `Approved`, `Implemented`, `Superseded`, `Archived`

Metadata `status` remains a bounded lifecycle value. Detailed task history belongs in the document body, not YAML. Existing long status strings in Phase 4-10 implementation reports become `Active`; their current summaries remain directly below the title.

Mandatory metadata applies to normative specifications, contracts, ADRs, standards, runbooks, implementation-status reports, and maintained design/plan artifacts. README and generated reports may use an explicitly documented exception because GitHub rendering and machine-generated files have different ownership rules.

Changelogs are mandatory for normative RFCs, standards, phase specifications, and phase contracts when a semantic version changes. Historical plans and implementation journals rely on git history and do not require per-file changelogs. Specification tags are required only for a phase promotion/freeze event, not for every approved planning artifact.

## Current-document reconciliation

The repair updates:

- `docs/00-document-control.md` to reference RFC-001 through RFC-005, STD-001, SPEC-000, architecture chapters, domain contracts, phase contracts, security, operations, and runbooks that exist.
- RFC-000 through RFC-004 metadata/status so approved downstream documents do not depend on Draft governing documents. The content is not rewritten beyond governance reconciliation and current-scope annotations.
- PHASE-002 from `Draft` to `Approved for Implementation`, matching its approved contracts and delivered implementation.
- README current status, ADR list, phase list, start order, evidence wording, and merged-branch references.
- Roadmap and phase index via generated blocks.
- Setup capability and limitation descriptions through Phase 10 Task 2.
- `.env.example` for embeddings, meeting-prep AI, personal-insight AI, and Gmail OAuth configuration.
- contributing guidance and PR checklist with `make docs-check`.

No document may describe an open dogfood, human review, production-readiness, or promotion gate as closed.

## Phase 10 contract set

Create these normative documents under `docs/phases/phase-010/`:

- `DATA-MODEL.md`: `email_threads`, `email_messages`, Gmail connector/account/cursor relationships, encryption boundaries, provenance, retention, deletion, indexes, and Task 1-2 implementation notes.
- `API-SCHEMAS.md`: OAuth start/callback and sync surfaces already delivered, plus clearly marked planned Task 3-8 endpoints and response/error contracts.
- `SYNC-CONTRACT.md`: 30-day backfill, Gmail history cursor semantics, pagination, deduplication, consent re-checks, rate limits, permission loss, entity linking, polling decision, and push-notification deferral.
- `PRIVACY-CONSENT-CONTRACT.md`: scopes, allowlist, encryption, on-demand body access, AI context, export, deletion, revocation, audit redaction, and CASA/public-rollout boundary.
- `UX-STATES.md`: disconnected, consent missing/expired, OAuth pending/error, syncing, partial/rate-limited, permission loss, empty, stale, body unavailable, deletion pending, and unsupported public rollout.
- `TEST-PLAN.md`: current Task 1-2 tests and required Task 3-8, real-account, backup/restore, privacy, adversarial, performance, accessibility, and recovery evidence.

The phase specification's `contracts` metadata names all six files. Current behavior and planned behavior are separated visibly in every contract.

## Evidence policy

Create `docs/evidence/README.md` defining acceptable evidence:

- committed test, benchmark, or recovery reports;
- immutable commit or pull-request URLs;
- GitHub Actions run/job URLs;
- committed human validation records;
- direct source/test references when the claim is mechanically re-runnable.

Uncommitted local agent reports are not durable evidence. Existing `.superpowers/sdd/*` citations are removed or replaced. When no durable evidence exists, the claim is downgraded to `unverified` rather than reconstructed from memory.

Phase 1 release and final-acceptance documents retain the historical explanation but no longer use missing files to support a checked gate.

## Production-readiness and recovery documentation

Create `docs/operations/PRODUCTION-READINESS.md` as the canonical cross-phase release-blocker register. Initial entries include:

- first production account/workspace owner provisioning;
- password reset/account recovery and MFA/step-up decision;
- connector-token and personal-data key rotation/re-encryption;
- connector/Gmail revocation and sync recovery;
- personal-data export, deletion, backup and restore evidence;
- automated post-deployment smoke checks;
- Phase 1, 3, and 5 human validation gates;
- Phase 6-10 promotion and independent review decisions.

Each entry has `status`, `affected capability`, `current safe behavior`, `required evidence`, `owner`, and `blocking scope`. Unknown owners use the repository owner, not an unassigned placeholder.

Create focused runbooks only for behavior that currently exists:

- `PHASE-6-CONNECTOR-RECOVERY.md`
- `PHASE-7-PERSONAL-DATA-RECOVERY.md`
- `PHASE-8-IDENTITY-RECOVERY.md`
- `PHASE-10-GMAIL-RECOVERY.md`

If key rotation, first-owner provisioning, password recovery, or MFA is not implemented, the relevant runbook says `Unsupported — production blocker`, describes safe containment, and points to the production-readiness record. It must not provide unsafe direct-SQL workarounds as supported procedures.

## KPI contract

Create `docs/product/KPI-CONTRACT.md`. It defines each existing RFC-001 metric with:

- decision supported;
- exact numerator/denominator or duration definition;
- population and time window;
- source and collection status;
- privacy classification;
- initial target or explicit `baseline_required` state;
- owner and review cadence.

No telemetry is added in this change. Metrics without a trustworthy source remain `not_collectable`; they are not reported as zero. Manual dogfood measurements retain their existing approved thresholds.

## Testing strategy

All executable changes follow test-first development.

1. Add failing unit tests for registry schema and state validation.
2. Add failing golden tests for generated Markdown blocks.
3. Add failing tests for missing metadata, invalid status, missing phase contracts, broken links/anchors, dead evidence references, and missing environment aliases.
4. Implement the minimum validator/generator behavior to pass.
5. Run the validator against the current repository; each surfaced failure is corrected in source documents rather than allowlisted unless this specification explicitly permits the exception.
6. Run focused Python tests, Ruff, formatting and mypy for the new scripts.
7. Run generated-status check and full documentation validation from a clean tree.

## Rollout

1. Land registry, generator, validator and tests.
2. Reconcile governance/status documents until validation passes.
3. Add Phase 10 contracts and operational/KPI records.
4. Enable the CI check only after the repository passes locally.
5. Record the first successful CI run as evidence in the pull request.

No historical dogfood or promotion gate is auto-closed by this rollout.

## Acceptance criteria

- One registry represents Phases 0-10 with separate specification, engineering, validation and promotion states.
- README, roadmap and phase index generated blocks match the registry.
- Phase 10 shows Tasks 1-2 complete and Tasks 3-8 open.
- PHASE-002 is no longer Draft.
- Phase 0-9 review is visibly archived as a historical snapshot.
- The documentation validator passes with no broken internal links/anchors, invalid governed metadata, unsupported governed statuses, missing approved-phase contracts, dead `.superpowers/sdd` evidence references, or missing runtime settings in `.env.example`.
- Phase 10 has all six supporting contracts.
- Production-readiness blockers and the four focused recovery runbooks exist without claiming unsupported operations work.
- KPI definitions distinguish collected, manual, baseline-required and not-collectable metrics.
- CI and `make docs-check` enforce the same checks.
- Unrelated repository changes remain untouched.

## Risks and mitigations

- **Registry becomes another duplicate.** Generated blocks and CI comparison make manual divergence a failure.
- **Validator overreaches into prose.** It validates structure and explicit contracts only; semantic implementation review remains human/change-review work.
- **Historical documents are rewritten inaccurately.** Historical reviews are archived and annotated, not retroactively converted into current reports.
- **Runbooks imply unsupported recovery.** Unsupported operations are named blockers with containment guidance only.
- **Large mechanical metadata changes obscure review.** Separate commits isolate validator, generated status, governance reconciliation, Phase 10 contracts, and operations/KPI documentation.
- **Upstream Phase 10 work continues during this repair.** Rebase before implementation and derive Phase 10 current state from the latest implementation-status report and code, not this document's initial commit snapshot.

## Changelog

| Version | Date | Summary | Author | Reference |
|---|---|---|---|---|
| 1.0.0 | 2026-08-06 | Approved governance-first documentation repair design | Lucky Jain | Documentation audit and owner approval |
