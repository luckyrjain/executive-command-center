---
id: EVENT-CATALOG
title: Domain Event Catalog
status: Approved
version: 1.5.0
owner: Lucky Jain
updated: 2026-10-01
related:
  - ADR-0005
  - DOMAIN-MODEL
  - PHASE-001
  - PHASE-002
---

# Domain Event Catalog

## Envelope

Every event is immutable and uses the canonical Phase 0 envelope. Events are past tense, schema version is part of `event_type`, consumers are idempotent by `event_id`, sensitive content is referenced rather than copied, and breaking payload changes create a new version.

## Phase 1 catalog

| Event | Producer | Required payload |
|---|---|---|
| `task.created.v1` | Planning | task_id, owner_id, status, priority |
| `task.updated.v1` | Planning | task_id, changed_fields |
| `task.completed.v1` | Planning | task_id, completed_at |
| `task.cancelled.v1` | Planning | task_id, reason |
| `task.archived.v1` | Planning | task_id, archived_at, pre_archive_status |
| `task.restored.v1` | Planning | task_id, restored_status |
| `commitment.created.v1` | Communication | commitment_id, direction, importance |
| `commitment.detected.v1` | Communication | commitment_id, evidence_ids, confidence |
| `commitment.confirmed.v1` | Communication | commitment_id, owner_id, due_date, due_at |
| `commitment.updated.v1` | Communication | commitment_id, changed_fields |
| `commitment.fulfilled.v1` | Communication | commitment_id, fulfilled_at |
| `commitment.cancelled.v1` | Communication | commitment_id, reason |
| `commitment.archived.v1` | Communication | commitment_id, archived_at, pre_archive_status |
| `commitment.restored.v1` | Communication | commitment_id, restored_status |
| `note.created.v1` | Knowledge | note_id, note_type, meeting_id |
| `note.updated.v1` | Knowledge | note_id, changed_fields, body_checksum |
| `note.archived.v1` | Knowledge | note_id, archived_at |
| `note.restored.v1` | Knowledge | note_id |
| `calendar_event.created.v1` | Planning | calendar_event_id, starts_at, ends_at |
| `calendar_event.changed.v1` | Planning | calendar_event_id, changed_fields |
| `meeting.created.v1` | Planning | meeting_id, calendar_event_id |
| `meeting.updated.v1` | Planning | meeting_id, changed_fields |
| `risk.identified.v1` | Executive Intelligence | risk_id, probability, impact, owner_id |
| `risk.updated.v1` | Executive Intelligence | risk_id, changed_fields |
| `risk.closed.v1` | Executive Intelligence | risk_id, closed_at |
| `attention_item.created.v1` | Executive Intelligence | attention_item_id, entity_ref, score, factors |
| `attention_item.updated.v1` | Executive Intelligence | attention_item_id, score, changed_factors |
| `recommendation.generated.v1` | Executive Intelligence | recommendation_id, source, evidence_ids, confidence |
| `recommendation.confirmation_requested.v1` | Executive Intelligence | recommendation_id, target_ref, target_version |
| `recommendation.accepted.v1` | Executive Intelligence | recommendation_id, accepted_by |
| `recommendation.rejected.v1` | Executive Intelligence | recommendation_id, rejected_by, reason |
| `recommendation.deferred.v1` | Executive Intelligence | recommendation_id, deferred_until |
| `recommendation.pinned.v1` | Executive Intelligence | recommendation_id, pinned_by |
| `recommendation.executed.v1` | Owning domain | recommendation_id, target_ref, resulting_version |
| `recommendation.failed.v1` | Owning domain | recommendation_id, error_code, retryable |
| `morning_brief.requested.v1` | Executive Intelligence | user_id, briefing_date, refresh_reason |
| `morning_brief.generated.v1` | Executive Intelligence | brief_id, user_id, evidence_ids, generation_version |
| `morning_brief.stale.v1` | Executive Intelligence | brief_id, stale_reason |
| `feedback.recorded.v1` | Executive Intelligence | feedback_id, recommendation_id, action |

Existing foundation events remain valid.

## Phase 2 catalog

Added incrementally, one entry per delivery slice, alongside the code that emits it (`docs/superpowers/plans/2026-07-21-phase-2-knowledge-platform.md`). Producer is Knowledge Platform (Person/Organization creation goes through the same producer -- see that plan's Task 1 -- since Identity is the domain owner per `DOMAIN-MODEL.md` but the event describes the shared `pkos_nodes` aggregate, not a separate Identity-owned table).

| Event | Producer | Required payload |
|---|---|---|
| `knowledge_entity.created.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.updated.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.archived.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.restored.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.claim_recorded.v1` | Knowledge Platform | entity_id, claim_id |
| `relationship.created.v1` | Knowledge Platform | relationship_id |
| `relationship.invalidated.v1` | Knowledge Platform | relationship_id |
| `resolution_candidate.created.v1` | Knowledge Platform | candidate_id |
| `resolution_candidate.confirmed.v1` | Knowledge Platform | candidate_id |
| `resolution_candidate.rejected.v1` | Knowledge Platform | candidate_id |
| `resolution_candidate.deferred.v1` | Knowledge Platform | candidate_id |
| `entity_operation.merged.v1` | Knowledge Platform | operation_id |
| `entity_operation.reversed.v1` | Knowledge Platform | operation_id |
| `entity_operation.split.v1` | Knowledge Platform | operation_id |

All Phase 2 catalog events are now implemented; no remaining speculative entries.

## Phase 3 catalog

Added incrementally, one entry per delivery slice, alongside the code that emits it (`docs/superpowers/plans/2026-07-22-phase-3-human-attention-engine.md`). `attention_item.created.v1`/`updated.v1` above are reused as-is for Task 1's extended `attention_items` -- not duplicated here.

| Event | Producer | Required payload |
|---|---|---|
| `waiting_link.opened.v1` | Executive Intelligence | waiting_link_id, version |
| `waiting_link.fulfilled.v1` | Executive Intelligence | waiting_link_id, version |
| `waiting_link.cancelled.v1` | Executive Intelligence | waiting_link_id, version |
| `risk_review.recorded.v1` | Executive Intelligence | risk_id, review_id |
| `plan.proposed.v1` | Executive Intelligence | plan_id |
| `plan.accepted.v1` | Executive Intelligence | plan_id, version |
| `plan.superseded.v1` | Executive Intelligence | plan_id, version |
| `meeting_pack.generated.v1` | Executive Intelligence | meeting_id |
| `meeting_pack.refreshed.v1` | Executive Intelligence | meeting_id |

## Phase 4 catalog

Added incrementally, one entry per delivery slice, alongside the code that emits it (`docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`). Producer is AI Runtime. `ai_prompt.activated`/`ai_tool.activated` (Task 2, `ecc.domains.ai_runtime.prompts`) are administrative-catalog audit events, not domain events describing a workspace aggregate, and are intentionally not listed in this table -- they follow `AUDIT-CONTRACT.md`'s audit-event convention, not this catalog's envelope.

| Event | Producer | Required payload |
|---|---|---|
| `ai_run.completed.v1` | AI Runtime | run_id, task_type, model_id, prompt_version |
| `ai_run.failed.v1` | AI Runtime | run_id, task_type, model_id, prompt_version, error_code |
| `ai_run.cancelled.v1` | AI Runtime | run_id, task_type, model_id, prompt_version |

`ai_run.failed.v1` also covers a run that finished `degraded` (design doc Decision 5: a total-wall-clock/output-token budget overrun) -- `DATA-MODEL.md` names three run-outcome events, not four, and the payload's `error_code` (`budget_exceeded`, `schema_invalid`, `tool_not_allowlisted`, `timeout`, `circuit_open`, `feature_disabled`, `remote_not_configured`, or an activation-specific extension) is what a consumer inspects to distinguish a hard failure from a degraded-but-terminated run. `ai_run_steps`' own per-step trace (never emitted as a domain event, matching `DATA-MODEL.md`'s redacted-trace convention) is where the finer-grained model-call/tool-call detail lives.

## Security remediation (Spec A) audit events

Connector ownership and personal-data isolation (security remediation Spec A; rollout `docs/runbooks/SPEC-A-ROLLOUT.md`). These events describe refusals and administrative side effects rather than a domain aggregate's own lifecycle. They are written through `audit_outbox.write_audit_and_outbox`, so each one is an `audit_events` row plus, unless noted, an `event_outbox` row stored as `<event>.v1`. Payloads carry ids, codes and reasons only: never an email address, a Google account id, a token or row content.

| Event (audit `event_type`) | Producer | Aggregate | Required payload | Notes |
|---|---|---|---|---|
| `connector_account.enrollment_refused` | Personal (Gmail OAuth callback); Engineering (connector reactivation) | `connector_account_enrollment` + fresh id when no row is involved (identity mismatch, membership inactive, insufficient role); otherwise `connector_account` + the existing row's id (owner conflict: **the other member's** connector; reactivation refusal: the disconnected row) | reason, provider | `authorization_result="denied"`; written in its own transaction after the business transaction rolled back. `reason` ∈ `identity_mismatch`, `owned_by_another_member`, `membership_inactive`, `insufficient_role` (callback), `not_found`, `access_denied` (reactivation). Also counted in `ecc_connector_enrollment_refused_total{provider,reason}` |
| `personal_data.share_refused` | Platform (grants, grant preview, ownership transfer, delegation create) | the refused row's own type and id | reason (`personal_data`), resource_type | `authorization_result="denied"`, own transaction. Only with `ECC_PERSONAL_DATA_ISOLATION` on. The path (`grant`, `grant_preview`, `transfer`, `delegation_create`) is on the metric `ecc_personal_data_share_refused_total{resource_type,path}`, not in the payload |
| `pkos_node.ownership_reassigned` | Identity (member removal) | `pkos_node` | aggregate_id, version, reason (`member_removed`), from_owner_id, to_owner_id | With `ECC_PERSONAL_DATA_ISOLATION` on only: a removed member's Gmail-only person node moved to the earliest other active workspace owner, in the removal transaction |
| `entity_alias.ownership_reassigned` | Identity (member removal) | `entity_alias` | aggregate_id, version, reason (`member_removed`), from_owner_id, to_owner_id | Flag on only: a Gmail-derived alias of such a node, moved with it |
| `connector_account.disabled` (new reasons) | Identity (member removal); ops (`scripts/remediate_connector_ownership.py`) | `connector_account` | aggregate_id, version, reason; plus check for remediation | The existing disable event, now also written when removal disconnects a member's personal connector (`reason: member_removed`; only with `ECC_PERSONAL_DATA_ISOLATION` on -- with it off, the member's personal rows block removal and nothing is disconnected) and when the operator remediates an S2 finding (`reason: operator_remediation`, `actor_id` null, `source: system`, metadata `check`, `ref_ids`, `run_id`). Neither path purges data |
| `connector_ownership.review_recorded` | ops (`scripts/remediate_connector_ownership.py`) | the flagged row's type and id | aggregate_id, check | **Audit only, no outbox event.** Records an operator's review of an S2 check B/D finding that needed no disconnect. Metadata: `reason` (`operator_remediation`), `check`, `table`, `ref_table`, `ref_id`, `decision` (`reviewed_no_identity_mismatch` / `reviewed_not_applicable`), `run_id` |

Workspace owners and admins read these, like every audit event, through `GET /api/v1/audit`. The dashboard's `recently_changed` brief shows every member the aggregate id, event type and changed field names (`changed_fields`) of recent ones, without content (an accepted disclosure, `docs/phases/phase-010/PRIVACY-CONSENT-CONTRACT.md`).

## Recommendation publication rule

`recommendation.generated.v1` records creation in `proposed`. `recommendation.confirmation_requested.v1` is emitted only by `PublishRecommendation`, which transitions the aggregate from `proposed` to `pending_confirmation`. Confirmation and execution events cannot occur before that publication event.

## Audit relationship

Domain events do not replace audit events. `AUDIT-CONTRACT.md` contains the normative API-action -> audit-event -> domain-event mapping. Successful mutations write the aggregate, redacted audit record and outbox event in one transaction. Rejected authorization and version-conflict attempts may create audit records without domain events.

## Compatibility and failure handling

Consumers support current versions and may support the previous version during migrations. Deprecated versions require migration and replay tests. Failed deliveries move to the dead-letter store with the original envelope, failure category, retry count and next action. Manual replay preserves `event_id` and creates a new delivery-attempt identifier.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.5.0 | 2026-10-01 | Security remediation Spec A (T19; review: removal events are flag-on only, `recently_changed` also shows `changed_fields`): catalogued `connector_account.enrollment_refused` (reasons), `personal_data.share_refused`, `pkos_node.ownership_reassigned`, `entity_alias.ownership_reassigned`, the new `connector_account.disabled` reasons and the audit-only `connector_ownership.review_recorded` | Lucky Jain |
