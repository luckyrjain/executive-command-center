---
id: SPEC-A-READINESS
title: Security Remediation Spec A Readiness Review Packet
status: Active
version: 1.0.0
owner: Lucky Jain
created: 2026-10-11
updated: 2026-10-11
depends_on:
  - SPEC-A-ROLLOUT
---

# Security Remediation Spec A readiness review packet

This packet is for the people who sign off Spec A and the operators who run it. It gathers, in one place, what each open sign-off item decides, where the decision is written down, what enforces it, which tests cover it, and what risk the signer is accepting. It signs nothing: the checklist boxes in [`SPEC-A-ROLLOUT.md`](SPEC-A-ROLLOUT.md#sign-off-checklist) are ticked by the people who own each decision, not by this document.

The detailed steps, SQL and rollback live in the rollout runbook. This packet links to them instead of repeating them, so there is one place to fix a step.

## 1. Where things stand (2026-10-11)

- Every Spec A task is on `main` except flag removal (T20), which waits for R7 plus two clean weeks. [`IMPLEMENTATION-STATUS.md`](../phases/phase-010/IMPLEMENTATION-STATUS.md) lists each PR.
- The three flags default to their safe values (`ECC_PERSONAL_DATA_ISOLATION=false`, `ECC_GMAIL_REQUIRE_IDENTITY_MATCH=false`, `ECC_GMAIL_REVOKE_SCOPE=global`). Deploying `main` changes nothing that is flag-gated.
- Unflagged changes are live on deploy: new error codes, search filtered by visibility, meeting packs holding only workspace-visible sources, guarded and truthfully counted revokes. See [Where things stand](SPEC-A-ROLLOUT.md#where-things-stand-2026-10-11) in the runbook.
- Sign-offs: only **A2** is signed (2026-09-30). The other nine items are open and gate R5.

## 2. What each rollout step changes

| Step | What happens | Who is affected |
|---|---|---|
| R1 | Deploy with all flags off. Migrations 0082, 0083 and 0084 apply (0083 and 0084 must be in place before R5). | Nobody sees flag-gated behaviour. Unflagged changes become visible. |
| R2 | Test whether Google revokes per token or per grant (D2). Decides `ECC_GMAIL_REVOKE_SCOPE`. | Operators only. |
| R3, R4 | Audit connector ownership, then remediate checks A, B, D and E with `scripts/remediate_connector_ownership.py`. | Affected connector owners get a notification. A and E rows are disconnected without purge. |
| R5 | Turn on `ECC_PERSONAL_DATA_ISOLATION`, run the backfill, rebuild knowledge projections. | Every member. Other members' Gmail connectors and Gmail-derived content disappear from their views. Sharing of that content is refused. Removal stops being blocked by it. |
| R6 | Turn on `ECC_GMAIL_REQUIRE_IDENTITY_MATCH`. | A member whose Google email differs from their ECC email can no longer connect Gmail (403 `GMAIL_ACCOUNT_IDENTITY_MISMATCH`). |
| R7 | 14 consecutive clean days, then T20 removes the flags. | Operators. |

## 3. R5 prerequisites, in order

All of these are checkboxes in [R5 Prerequisites](SPEC-A-ROLLOUT.md#prerequisites-all-must-hold). Do them in this order; the SQL is in the runbook.

1. **R4 gate met.** Migrations 0083 and 0084 applied, alert rules live, every sign-off ticked.
2. **Database backup**, verified, with its name in the change record.
3. **Record two deploy times**, written with an explicit UTC offset:
   - `<fx1_deploy_time>`: when the FX1 build (#308) went live;
   - `<fx10_deploy_time>`: when the build with prep-summary AI runs private to their actor (#399) went live.
4. **Confirm the FX7 (#396), FX10 (#399) and DS2 (#403) builds are live.** This must hold before any statement below runs: it is what makes meeting-pack reads drop narrowed rows, what makes new prep-summary runs private from the start, and what makes members who withdraw afterwards get redacted by the cascade. Running the narrowing first would race with runs an older build is still writing.
5. **Snapshot, then clean up the meeting-prep data.** The snapshot, the SQL pack archive and the AI-run narrowing are one transaction in one `psql` session:
   - archive every active meeting pack generated before `<fx1_deploy_time>` with the runbook's SQL, in that transaction; or refresh them through the API **after** the `COMMIT` (the snapshot still lists which packs);
   - narrow every `meeting.prep_summary` AI run created before `<fx10_deploy_time>` to its actor, and revoke grants on them.
   After the commit, run the check for any prep-summary run still `workspace`-visible. If it is not 0, run the `_rolling` block, but only once no pre-FX10 process is left.
6. **DS2 one-off.** Redact email AI content (runs, steps, cached replay records, `proposed_fields`) for members who already withdrew email consent. Record the `members_in_scope` count (members with an email run and email off, including ones already redacted). This one is irreversible by design, so it takes no snapshot; run it in a quiet period.

Then follow [Order](SPEC-A-ROLLOUT.md#order) (flag on and restart, backfill, rebuild per workspace, refresh packs), [Verify](SPEC-A-ROLLOUT.md#verify), and know the [Rollback](SPEC-A-ROLLOUT.md#rollback-asymmetric): turning the flag off alone is not a rollback.

## 4. Sign-off evidence

Each item below answers four questions: what exactly is being accepted, where it is written down, what enforces it, and which tests cover it. **Test mappings marked "inferred" are matched by content: no test or code carries the DS1, C15-c, I5, N27, N28 or N29(1) tags.**

### DS3 (privacy): Gmail-derived content is private; correspondents stay workspace knowledge

- **Decision**
  - Gmail-derived content (threads, messages, evidence, recommendations, email AI runs and the rows derived from them) is private to the mailbox owner.
  - Person entities and their aliases created from Gmail stay workspace knowledge, so entity resolution keeps working across the workspace.
- **Written in**
  - [Privacy and consent contract](../phases/phase-010/PRIVACY-CONSENT-CONTRACT.md): "Correspondent directory (DS3)" and "Accepted admin capabilities and residual disclosures".
  - Phase 10 [API schemas](../phases/phase-010/API-SCHEMAS.md): the "Unchanged on purpose" note.
- **Enforced by**
  - `gmail_adapter.py` (`resolve_or_create_person`), and `connector_security.py` (`personal_knowledge_owner`, `personal_content_scope`, `personal_derived_row_scope`).
  - `membership_removal.py` (`_reassign_nodes`, `_reassign_gmail_aliases`).
  - `scripts/backfill_personal_visibility.py`.
- **Tests**
  - `tests/test_personal_data_private_writes_postgres.py`: `test_flag_on_aliases_owned_by_mailbox_owner_and_stay_workspace`, `test_flag_on_sync_writes_content_private_to_mailbox_owner`, `test_flag_on_other_members_cannot_list_read_or_act`.
  - `tests/test_evidence_readers_visibility_postgres.py`: `test_retrieval_body_and_evidence_state_exclude_private_evidence`, `test_citing_unreadable_evidence_is_rejected_as_not_found`.
- **Residuals the signer must acknowledge** (quoted from the contract)
  - **N28:** workspace owners and admins can read full row snapshots (`before` and `after`) through `GET /api/v1/audit`, including private, email-derived content, unredacted.
  - **N27:** members see the aggregate id, event type and changed field names of other members' changes in the dashboard's `recently_changed`. No content.
  - **N29(1):** knowledge claims stay visible as their author wrote them, even when they cite another member's private Gmail evidence.
  - Members can see that a correspondent's email address exists in the workspace graph, and with few Gmail users can infer whose mailbox it came from.
  - References that carry ids only, and Gmail evidence whose mailbox owner is ambiguous (that evidence stays as written, usually workspace-visible).
- **Coverage gap:** no test asserts the N27 ids-only behaviour, the N28 unredacted snapshots, or N29(1). Those three residuals are documented, not pinned.

### DS1 (product): no Gmail aliases; a different work email is refused with guidance

- **Decision:** with `ECC_GMAIL_REQUIRE_IDENTITY_MATCH` on, the Google account's email must equal the member's ECC account email, or the connect is refused with 403 `GMAIL_ACCOUNT_IDENTITY_MISMATCH` and nothing is written.
- **Written in**
  - Phase 10 API schemas: the Gmail callback errors list.
  - [Connector contract](../phases/phase-006/CONNECTOR-CONTRACT.md): "One owner per mailbox".
  - [Rollout runbook R6](SPEC-A-ROLLOUT.md#r6-identity-binding), and [`docs/SETUP.md`](../SETUP.md) (the three Gmail settings).
- **Guidance to the member:** the Gmail panel shows "The Google account you chose isn't the one signed in to ECC. Nothing was connected -- connect again and choose the Google account you use to sign in to ECC." (`frontend/src/features/personal/errors.ts`).
- **Enforced by:** `gmail_oauth.py` (`gmail_oauth_callback_endpoint`, `_identity_matches`, `_caller_email`, `_refuse_enrollment`); `config.py` (default `False`).
- **Tests** (`tests/test_gmail_identity_binding_postgres.py`, inferred): `test_identity_mismatch_is_refused_without_revoking_a_live_grant`, `test_missing_caller_email_is_refused_as_a_mismatch`, `test_flag_off_keeps_accepting_another_google_account`.
- **Residual:** until R6, with the flag off, a mismatched Google account is accepted.

### DS2 (privacy and product): retain a removed member's Gmail-derived data, private; redact on consent withdrawal

- **Decision** (the removal rules take effect with `ECC_PERSONAL_DATA_ISOLATION` on, at R5. Until then removal behaves as before: Gmail rows block it with 409 and nothing is disconnected or re-owned. The consent-withdrawal redaction runs regardless of the flag.)
  - Removing a member does not purge their Gmail-derived data: it is retained, private to them, and no longer blocks the removal. Their Gmail connectors are disconnected and the Google grant revoked where that is safe.
  - Withdrawing email consent runs the cascade, which also redacts the member's `email.detect_action` AI runs and steps in place, deletes the cached replay records for email detection, and clears `proposed_fields` on executed recommendations (decided 2026-10-11).
  - A removed member's own prep-summary AI runs and steps are retained private and do not block removal, with the flag on (#402).
- **Written in:** privacy contract "Member removal retains, it does not purge" and the consent revocation section; Phase 10 API schemas (the `DELETE .../members/{user_id}` row); runbook limitation "The consent cascade redacts, not deletes" and the R5 "DS2 one-off".
- **Enforced by:** `gmail_revocation.py` (`cascade_email_revocation`), `membership_removal.py` (`_disconnect_personal_connectors`, `remove_member_endpoint`), `authz.py` (`_PERSONAL_ROWS_NOT_BLOCKING_REMOVAL`, `ACTOR_PRIVATE_ROW_PREDICATES`).
- **Tests**
  - `tests/test_gmail_revocation_postgres.py`: `test_consent_withdrawal_redacts_email_ai_runs_and_steps_keeping_the_skeleton` (disable, revoke and delete), `test_disable_domain_redacts_executed_recommendation_instead_of_deleting`.
  - `tests/test_identity_member_removal_personal_data_postgres.py`: `test_active_gmail_disconnected_audited_and_revoked_after_commit`, `test_member_with_only_disconnected_gmail_and_personal_rows_is_removable`, `test_flag_off_personal_rows_still_block_removal`.
  - `tests/test_personal_data_private_writes_postgres.py`: `test_flag_on_mailbox_owner_removable`.
  - `tests/test_meeting_prep_visibility_postgres.py`: `test_prep_summary_runs_block_removal_only_without_isolation`.
- **Residuals**
  - Removal does not run the consent cascade, so a removed member's AI runs keep their content, private to them.
  - AI runs from before the DS2 deploy are redacted by the R5 one-off only for members whose email domain is off; a member who withdrew and later re-enabled is redacted at their next withdrawal.
  - A retained run can still be shared or transferred by an owner or admin who knows its id (no API lists run ids). A re-added member regains their old runs.
  - At about 2M owned email recommendations, the removal count can exceed the 5 s timeout (6.9 s measured).
  - The privacy contract still labels the removal rule "default pending sign-off" until this item is ticked.

### Q-A1 (security): no admin break-glass for another member's Gmail; use removal

- **Decision:** owners and admins get no read access to another member's Gmail connector or Gmail-derived rows through the domain endpoints. The route is member removal.
- **Written in:** privacy contract ("There is no admin break-glass; the route is member removal"); connector contract "Owner-only personal connectors"; Phase 10 API schemas.
- **Enforced by:** `connector_accounts.py` (`_deny_non_owner_personal_connector`, `_is_non_owner_personal_connector`), `connector_security.py` (`personal_data_share_guard`, `refuse_personal_data_share`), and private row visibility in `authz.py`.
- **Tests:** `tests/test_personal_connector_isolation_postgres.py` (`test_non_owner_cannot_see_connector_or_runs`, `test_non_owner_mutation_is_404_and_counted`); `tests/test_engineering_connector_reactivation_authz_postgres.py` (`test_admin_or_workspace_owner_cannot_reactivate_another_members_private_row`); `tests/test_identity_member_removal_personal_data_postgres.py` (`test_admin_removes_member_and_admin_gmail_is_untouched`); `tests/test_personal_data_isolation_postgres.py` (`test_flag_on_refuses_sharing_personal_row`).
- **Residual:** the audit log (N28 above) is the one admin read path. Sharing is refused for the owner too.

### C15-a (security): any member may reactivate a workspace engineering connector with their own credentials

- **Decision:** a `workspace`-visible engineering connector grants write to every member, so any member may reactivate it with their own credentials. The action is audited as `connector_account.reconnected`, naming the actor.
- **Written in:** connector contract "Reactivation authz"; Phase 10 API schemas (`POST /api/v1/engineering/connectors`).
- **Enforced by:** `connector_accounts.py` (the "Accepted limitation (decision C15-a)" comment, `_reactivation_refusal_guard`).
- **Tests** (`tests/test_engineering_connector_reactivation_authz_postgres.py`): `test_reactivation_of_workspace_row_by_another_member_is_allowed`, `test_reactivation_with_read_only_grant_is_403_and_audited`, `test_row_owner_may_always_reactivate`.
- **Residual:** any member can take over a workspace-visible engineering connector with their own credentials.

### C15-c (product): per-workspace uniqueness of `(provider, external_account_id)` kept

- **Decision:** one connector row per provider account per workspace; a second member trying to connect the same mailbox gets 409 `CONNECTOR_OWNED_BY_ANOTHER_MEMBER`.
- **Written in:** connector contract "One owner per mailbox" and "Integrity errors"; Phase 10 API schemas (callback errors).
- **Enforced by:** migration `0044` (`uq_connector_accounts_workspace_provider_external_id`), `gmail_oauth.py`, `connector_security.is_unique_violation`.
- **Tests** (inferred): `tests/test_gmail_owner_conflict_postgres.py` (`test_foreign_owned_active_conflict_is_refused_without_revoking_under_global_scope`, `test_foreign_owned_disconnected_row_is_not_taken_over`, `test_non_conflicting_account_for_another_member_is_created`); `tests/test_platform_connector_security_postgres.py` (`test_is_unique_violation_matches_only_the_named_constraint`).
- **Residual:** under `global` revoke scope, extra Google grants can stay (see section 6).

### C15-d (privacy): a removed member's email consent stays granted

- **Decision:** with `ECC_PERSONAL_DATA_ISOLATION` on (R5), removal disconnects the connector but leaves the member's `email` consent granted; with the connector disconnected, nothing syncs. Until R5, removal still blocks on Gmail rows and disconnects nothing.
- **Written in:** privacy contract (end of the removal paragraph).
- **Enforced by:** `membership_removal.py` (`_disconnect_personal_connectors`, "no purge, `domain_consents` unchanged"); the sync refusal `403 MEMBERSHIP_INACTIVE` for a removed member (`403 EMAIL_CONSENT_NOT_ACTIVE` does not apply: their consent is still granted).
- **Tests:** `tests/test_identity_member_removal_personal_data_postgres.py`: `test_active_gmail_disconnected_audited_and_revoked_after_commit` (asserts the consent row is still unrevoked); `test_member_with_only_disconnected_gmail_and_personal_rows_is_removable` covers removal only and makes no consent assertion.
- **Residual:** the protection is the disconnected connector, not revoked consent.

### C15-f (privacy): remediation notifies the row owner only

- **Decision:** `scripts/remediate_connector_ownership.py` notifies only the active owner of a remediated row. Admins and other members get nothing, and a removed owner is not notified.
- **Written in:** the script's module docstring ("Spec A C15-f: the row owner only"). No contract states it yet, so this packet is its only prose statement outside the code.
- **Enforced by:** `_notify_owner`, which inserts a `connector_account.disconnected` notification only when the owner is an active member.
- **Tests** (`tests/test_remediate_connector_ownership_postgres.py`): `test_a_and_e_disconnect_without_purge_and_revoke_iff_safe`, `test_b_and_d_record_review_or_disconnect_when_mismatched_from_audit_csv`.

### I5 (security): identity binding proves an email match, not mailbox ownership

- **Decision:** the binding is a normalised string comparison of the Google account email and the member's ECC account email. There is no email-verification step in the backend (a search for `email_verified`, `verified_at`, `verify_email` and "email verification" finds nothing), so it relies on the ECC account email being trustworthy.
- **Written in:** the runbook checklist line only, plus the mechanism in the API schemas and [R6](SPEC-A-ROLLOUT.md#r6-identity-binding).
- **Enforced by:** `gmail_oauth.py` (`_identity_matches`).
- **Tests** (inferred): `tests/test_gmail_identity_binding_postgres.py`: `test_identity_match_connects_normally`, `test_identity_mismatch_is_refused_without_revoking_a_live_grant`.
- **Residual:** the check shows the two emails match. It does not show that the member owns the mailbox, because ECC does not verify the account email. For context: creating an account (`POST /accounts`) requires a valid invitation token whose email matches, and a search of the backend finds no statement that updates an account's email afterwards. How invitations are issued is outside this packet.

## 5. Decisions already made (not sign-offs)

| Decision | When | Where recorded |
|---|---|---|
| **A2:** a backfill re-owning a derived row to an inactive recommendation owner is reported as `owner_inactive`; keep current behaviour. | 2026-09-30 (signed) | Runbook checklist |
| **Owners may not share their own email-derived rows.** The refusal (grant, transfer, delegation and previews) stays. If sharing is wanted later it would be a separate, explicit per-item action. | 2026-10-11 | Privacy contract "Not shareable"; runbook footer |
| **The consent cascade redacts, not deletes,** email AI runs and steps. | 2026-10-11 | Runbook limitations; privacy contract |
| **A removed member's private prep-summary runs do not block removal** (flag on). | 2026-10-11 | Runbook; API schemas |

## 6. Accepted residuals, cross-cutting

These are in the runbook's [Known limitations](SPEC-A-ROLLOUT.md#known-limitations-accepted-not-blockers). The signers accept them together with the items above:

- Under `global` revoke scope, replaced or duplicate Google grants are not revoked while a live row uses the account, and a rejection whose Google email is unknown is never revoked. Members can remove them at Google.
- The backfill log stores member-written attention `override_reason` text as an unsalted SHA-256 digest (never the text). Restrict read access to the log and purge it after R7.
- Stored meeting packs keep narrowed rows until refreshed; reads drop them, and the stored summary is withheld.
- A manual Gmail sync holds the owner's connector row locked during a token refresh (up to 10 s), so other work for that owner can hit the 5 s statement timeout. It is safe to retry.
- Migration 0083 has no `lock_timeout`; run R1 in a maintenance window.
- For DS1, C15-c and I5, the contracts name these IDs only in the privacy contract's sign-off status line; none of them states the decision text. The wording above is taken from the runbook checklist and from the behaviour.

## 7. Sign-off record

Fill this in as each item is signed. Record who signed, when, and where (a link or change-record id), then tick the matching box in the [rollout runbook](SPEC-A-ROLLOUT.md#sign-off-checklist). This packet is not updated automatically.

| Item | Signed by | Date | Reference |
|---|---|---|---|
| A2 | the user | 2026-09-30 | Runbook checklist |
| DS3 | | | |
| DS1 | | | |
| DS2 | | | |
| Q-A1 | | | |
| C15-a | | | |
| C15-c | | | |
| C15-d | | | |
| C15-f | | | |
| I5 | | | |

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-10-11 | First readiness packet: status, step impact, ordered R5 prerequisites, evidence per sign-off item, decisions already made, cross-cutting residuals, sign-off record | Lucky Jain |
