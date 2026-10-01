---
id: SPEC-A-ALERTS
title: Security Remediation Spec A Alert Rules
status: Active
version: 1.4.0
owner: Lucky Jain
created: 2026-10-01
updated: 2026-10-01
depends_on:
  - SPEC-A-ROLLOUT
  - PHASE-10-GMAIL-RECOVERY
---

# Security Remediation Spec A alert rules

This repository has no alerting stack and no other alert rules. These are the Prometheus rules for the connector-ownership and personal-data-isolation counters (security remediation Spec A), for the operator to load into whatever Prometheus/Alertmanager watches the deployment. They are needed from rollout step R2 (the D2 canary) and must be live for the two clean weeks of R7 (see [`SPEC-A-ROLLOUT.md`](../runbooks/SPEC-A-ROLLOUT.md)).

## Where the numbers come from

- `GET /metrics` on every backend process (`backend/ecc/main.py`) serves the Prometheus text format. When `ECC_METRICS_TOKEN` is set, the scraper must send `Authorization: Bearer <token>`.
- The counters are hand-rolled and **process-local** (`backend/ecc/observability.py`). Each API worker or replica must be scraped as its own target, and every counter goes back to zero when its process restarts. Each flag change in the rollout restarts every process.
- **Every bounded label set exists at 0 from process start** (#355). `ecc.main` calls `observability.preinitialise_connector_security_counters` at import. It creates every combination of the literal labels in the table below, for `provider` ∈ `PERSONAL_PROVIDERS` (today `gmail`) and `resource_type` ∈ `connector_security.SHARE_REFUSED_RESOURCE_TYPES`, all at 0. A first event after a restart is therefore a 0 → 1 step that `increase()` sees, and the restart itself is a drop that `increase()` treats as a counter reset. Every rule below is plain `increase()`.
  - **Note: why 1.1.0 to 1.3.0 had new-series terms.** Before #355, `_Counter.inc` created each label set lazily at 1, and the text format carries no created timestamp, so `increase()` never saw the event that created a series. Those versions added `X unless last_over_time(X[55m] offset 5m)` to the rare-event rules to catch a series that had just appeared. That term had false positives (a target down for over 55 minutes, a relabelling, a fresh TSDB) and still missed a restarted process that reached the old process's value. Pre-initialisation replaced it; do not bring it back unless pre-initialisation is removed.
  - **Remaining gaps.** (1) An event between a process start and that process's **first scrape** is missed if the counter then shows the same value the previous process last reported (no drop, no step). The window is one scrape interval per restart. (2) Label sets outside the pre-initialised sets, mainly a non-personal (engineering) `provider` on `ecc_connector_revoke_total` or `ecc_connector_enrollment_refused_total`, are still created lazily at 1, so their first event after a restart is missed. Engineering connectors' `disconnect()` is a documented no-op, so a revoke `error` there is not expected. After each restart, still check the audit log and the change record's ops-script counts. Never alert on a raw counter value.
- Ops scripts are one-off processes that nobody scrapes: `scripts/remediate_connector_ownership.py` (`site="remediation"`) and `scripts/backfill_personal_visibility.py`. Their counts appear only in their own stderr summary and CSV, so copy them into the change record.
- No label carries an email, account id, workspace id or credential, so an alert names a provider, a site or a reason, never a person. To find the affected row, use the matching audit events (`GET /api/v1/audit`, owner/admin only) at the alert's time.

| Counter (exact name) | Labels | Emitted by |
|---|---|---|
| `ecc_connector_revoke_total` | `provider`, `site` ∈ {`callback_failure`, `callback_duplicate`, `reconnect_replaced`, `disable`, `cascade`, `removal`, `adapter_callback`, `remediation`}, `result` ∈ {`ok`, `error`, `skipped_unsafe`} | every provider-side revoke (`connector_security.revoke_guarded`) |
| `ecc_connector_enrollment_refused_total` | `provider`, `reason` ∈ {`identity_mismatch`, `owned_by_another_member`, `not_found`, `access_denied`, `membership_inactive`, `insufficient_role`} | Gmail OAuth callback refusals; engineering connector reactivation refusals (`not_found`, `access_denied`) |
| `ecc_personal_data_share_refused_total` | `resource_type`, `path` ∈ {`grant`, `grant_preview`, `transfer`, `delegation_create`} | share/transfer/delegation of personal or email-derived rows, flag on |
| `ecc_connector_access_denied_total` | `provider`, `route` ∈ {`sync`, `disable`} | a non-owner trying to sync or disable another member's Gmail connector, flag on |
| `ecc_gmail_refresh_rejected_total` | `error` ∈ {`invalid_grant`, `other`}, `since_reconnect` ∈ {`lt_1h`, `1h_24h`, `gt_24h`, `unknown`} | a failed Gmail access-token refresh |

## Reading the series

- **`result="error"` is truthful** (Security Remediation FX6). It means Google refused the revoke, the transport failed, or the stored credential was unusable, so the grant **may still be live**. `ok` means Google returned a 2xx or HTTP 400 `invalid_token`, meaning the token is already revoked, expired or unknown.
- **A failed revoke-safety check usually counts as `error`.** When the `revoke_is_safe` query itself fails (a database error), the revoke is not attempted (fail closed). At the `revoke_if_safe` sites -- `callback_failure`, `callback_duplicate`, `reconnect_replaced`, `disable`, `cascade`, `remediation` -- that is counted `result="error"` and logged `connector_revoke_safety_check_failed provider=<p> site=<s> error_class=<C>`; the grant was not revoked, so treat it like any other error.
- **`skipped_unsafe` has two meanings.** Usually `revoke_is_safe` found another live connection to the same Google account (expected under the default `ECC_GMAIL_REVOKE_SCOPE=global`). At two sites it also counts a revoke-safety check that itself failed, which fails closed and is **not** counted as `error`: `site="adapter_callback"` (log `gmail_revoke_on_reject_check_failed: error_class=<C>`) and `site="removal"` (log `removal_revoke_safety_check_failed error_class=<C>`). Read a `skipped_unsafe` rise at those sites as "maybe DB trouble" too, and check those log lines.
- **Every Gmail callback refusal also revokes at `site="callback_failure"`.** That covers `identity_mismatch`, `owned_by_another_member`, `membership_inactive` and `insufficient_role`. The minted grant is revoked iff safe. Under `global`, an owner-conflict refusal counts `skipped_unsafe` while the other member's row is live (not `disconnected`); if that row is disconnected and no other live row anywhere uses the Google account, the minted grant is revoked and counted `ok` or `error`. So `callback_failure` volume tracks refusals. Net the refusals out before treating a `callback_failure` trend as an adapter problem (recording rule below).
- **The refresh canary only sees manual syncs.** Gmail sync is manual-only, and a refresh happens only when a sync finds the access token expired, at least about 59 minutes after connect. The spec's original rule (`since_reconnect="lt_1h"` > 0) can never fire, and a duplicate-callback revoke writes no reconnect audit row to bucket against. The canary is therefore `invalid_grant` in **any** bucket, above its own baseline (plan note N18). Detection latency is "the next manual sync of an affected connector after its access token expires". It does not count Gmail API 401s during a sync, so the R2 check in the rollout runbook also watches sync runs.

## Rules

```yaml
groups:
  - name: ecc-spec-a-recording
    rules:
      # Revokes deliberately not attempted, per site, for the post-D2 trend review.
      - record: ecc:connector_revoke_skipped_unsafe:increase1d
        expr: sum by (provider, site) (increase(ecc_connector_revoke_total{result="skipped_unsafe"}[1d]))
      # Gmail callback-failure revokes NOT explained by an enrollment refusal
      # (each refusal also revokes at site="callback_failure"). Roughly the
      # adapter/persist failures; a sustained rise is worth a look.
      - record: ecc:gmail_callback_failure_revokes_net_of_refusals:increase1d
        expr: |
          sum(increase(ecc_connector_revoke_total{provider="gmail",site="callback_failure"}[1d]))
            - (sum(increase(ecc_connector_enrollment_refused_total{provider="gmail"}[1d])) or vector(0))

  - name: ecc-spec-a-alerts
    rules:
      # Any revoke whose Google grant may still be live. Truthful for Gmail since FX6.
      - alert: EccConnectorRevokeFailed
        expr: sum by (provider, site) (increase(ecc_connector_revoke_total{result="error"}[1h])) > 0
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.provider }} revoke failed at site {{ $labels.site }}; the provider grant may still be live"
          runbook: "docs/runbooks/PHASE-10-GMAIL-RECOVERY.md#google-revoke-failed-ecc_connector_revoke_totalprovidergmailresulterror"

      # The spec's ratio rule: more than 20% of attempted revokes failed in a day.
      # Denominator = attempted revokes (ok + error); skipped_unsafe were never attempted.
      - alert: EccConnectorRevokeErrorRatioHigh
        expr: |
          (
            sum by (provider) (increase(ecc_connector_revoke_total{result="error"}[1d]))
              / sum by (provider) (increase(ecc_connector_revoke_total{result=~"ok|error"}[1d]))
          ) > 0.2
        labels:
          severity: critical
        annotations:
          summary: "More than 20% of {{ $labels.provider }} revokes failed over the last day"
          runbook: "docs/runbooks/PHASE-10-GMAIL-RECOVERY.md#google-revoke-failed-ecc_connector_revoke_totalprovidergmailresulterror"

      # D2 / refresh canary (plan note N18): invalid_grant in ANY since_reconnect
      # bucket above its baseline (twice the daily average of the previous week;
      # with a zero baseline, any invalid_grant fires).
      - alert: EccGmailRefreshInvalidGrantAboveBaseline
        expr: |
          sum(increase(ecc_gmail_refresh_rejected_total{error="invalid_grant"}[1d])) > 0
          and
          sum(increase(ecc_gmail_refresh_rejected_total{error="invalid_grant"}[1d]))
            > 2 * ((sum(increase(ecc_gmail_refresh_rejected_total{error="invalid_grant"}[7d] offset 1d)) / 7) or vector(0))
        labels:
          severity: warning
        annotations:
          summary: "Gmail refreshes rejected with invalid_grant above baseline: a revoke may have killed a live grant (D2 canary)"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#r2-d2-revoke-scope-test"

      # A spike of refused shares of personal / email-derived rows after R5:
      # more than 5 in an hour, unless that is within 3x the hourly average of
      # the last week for the same path (no history for the path -> fires).
      - alert: EccPersonalDataShareRefusedSpike
        expr: |
          sum by (path) (increase(ecc_personal_data_share_refused_total[1h])) > 5
          unless on (path)
          sum by (path) (increase(ecc_personal_data_share_refused_total[1h]))
            <= 3 * sum by (path) (increase(ecc_personal_data_share_refused_total[7d] offset 1h)) / 168
        labels:
          severity: warning
        annotations:
          summary: "Spike of refused {{ $labels.path }} attempts on personal data"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#monitoring"

      # Spec § Observability: any identity mismatch (only emitted once
      # ECC_GMAIL_REQUIRE_IDENTITY_MATCH is on, rollout R6).
      - alert: EccGmailIdentityMismatchRefused
        expr: sum(increase(ecc_connector_enrollment_refused_total{provider="gmail",reason="identity_mismatch"}[1d])) > 0
        labels:
          severity: info
        annotations:
          summary: "A Gmail connect was refused because the Google account differs from the member's own email"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#monitoring"

      # Someone tried to connect a mailbox another member already owns.
      - alert: EccGmailOwnerConflictRefused
        expr: sum(increase(ecc_connector_enrollment_refused_total{provider="gmail",reason="owned_by_another_member"}[1d])) > 0
        labels:
          severity: info
        annotations:
          summary: "A Gmail connect was refused: the mailbox is already connected by another member"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#monitoring"

      # A non-owner tried to sync or disable another member's Gmail connector (flag on).
      - alert: EccPersonalConnectorAccessDenied
        expr: sum by (provider, route) (increase(ecc_connector_access_denied_total[1d])) > 0
        labels:
          severity: info
        annotations:
          summary: "Non-owner {{ $labels.route }} of a personal {{ $labels.provider }} connector was refused"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#monitoring"
```

Every rule is plain `increase()`, so an alert stays firing while its event is inside the rule's window: 1 hour for `EccConnectorRevokeFailed`, 1 day for the identity-mismatch, owner-conflict, access-denied and canary rules.

Thresholds (5 an hour, 3x, 2x, 20%) are starting points for a small internal deployment. Tune them after the first clean week, and record the change here.

## Rule unit test (promtool)

Save the rules block above as `spec-a-alerts.rules.yml` and this as `spec-a-alerts.test.yml`, then run `promtool test rules spec-a-alerts.test.yml` wherever promtool is installed (it is not part of this repository's toolchain). The first case is the one that matters: a fresh process exposes the pre-initialised series at 0, then counts one revoke error, which `increase()` sees as 0 → 1. The second is a restart with a lower count, which `increase()` catches as a reset. The third is an existing series with a staleness marker and a 10-minute gap, then the same value, which must not fire.

```yaml
rule_files:
  - spec-a-alerts.rules.yml
evaluation_interval: 1m
tests:
  # A revoke error in a fresh process: the pre-initialised series is 0 from
  # the first scrape, then the event takes it to 1 (first 1 at 91m).
  - interval: 1m
    input_series:
      - series: 'ecc_connector_revoke_total{provider="gmail",site="removal",result="error",instance="api-1"}'
        values: '0x90 1x120'
    alert_rule_test:
      - eval_time: 80m          # before the event: nothing
        alertname: EccConnectorRevokeFailed
        exp_alerts: []
      - eval_time: 92m          # first event: 0 -> 1 is an increase
        alertname: EccConnectorRevokeFailed
        exp_alerts:
          - exp_labels: {severity: warning, provider: gmail, site: removal}
            exp_annotations:
              summary: "gmail revoke failed at site removal; the provider grant may still be live"
              runbook: "docs/runbooks/PHASE-10-GMAIL-RECOVERY.md#google-revoke-failed-ecc_connector_revoke_totalprovidergmailresulterror"
      - eval_time: 155m         # the step has left the 1h window: resolved
        alertname: EccConnectorRevokeFailed
        exp_alerts: []
  # A restart: the old process had counted 3, the new one counts 1.
  - interval: 1m
    input_series:
      - series: 'ecc_connector_revoke_total{provider="gmail",site="cascade",result="error",instance="api-1"}'
        values: '3x30 _x30 1x60'
    alert_rule_test:
      - eval_time: 70m          # the drop from 3 to 1 is a reset: increase() > 0
        alertname: EccConnectorRevokeFailed
        exp_alerts:
          - exp_labels: {severity: warning, provider: gmail, site: cascade}
            exp_annotations:
              summary: "gmail revoke failed at site cascade; the provider grant may still be live"
              runbook: "docs/runbooks/PHASE-10-GMAIL-RECOVERY.md#google-revoke-failed-ecc_connector_revoke_totalprovidergmailresulterror"
  # An old series with a staleness marker and a 10-minute scrape gap, then the
  # same value again: no new event, so no alert.
  - interval: 1m
    input_series:
      - series: 'ecc_connector_revoke_total{provider="gmail",site="disable",result="error",instance="api-1"}'
        values: '1x30 stale _x9 1x60'
    alert_rule_test:
      - eval_time: 45m          # back after the gap at the same value
        alertname: EccConnectorRevokeFailed
        exp_alerts: []
      - eval_time: 90m
        alertname: EccConnectorRevokeFailed
        exp_alerts: []
```

This test has not been run here: promtool is not available in this environment. Run it before loading the rules.

## Revoke-error evidence

Every path that counts `ecc_connector_revoke_total{result="error"}` writes at least one of these log lines (process log of the API worker that did the revoke; the remediation script logs to its own stderr). Audit events are **not** a reliable signal: several sites write none.

| Site(s) | Log line(s) that accompany the `error` | Audit event to look for |
|---|---|---|
| any site through `revoke_guarded` (`callback_failure`, `callback_duplicate`, `reconnect_replaced`, `disable`, `cascade`, `removal`, `remediation`) | `connector_revoke_failed provider=<p> site=<s> error_class=<C>`; for Gmail also `gmail_revoke_failed: reason=<reason> status=<status>` (plus `gmail_revoke_post_failed: error_class=<C>` on a transport failure) | `disable`: `connector_account.disabled`; `removal`: `connector_account.disabled` (`member_removed`); `remediation`: `connector_account.disabled` (`operator_remediation`); `cascade`: the email consent revoke / domain disable or delete audit; `callback_failure`: `connector_account.enrollment_refused` for a refusal, **none** for an adapter or persistence failure; `callback_duplicate`, `reconnect_replaced`: **none required** (a reconnect may have its own connector event) |
| `revoke_if_safe` sites (all of the above except `removal`) when the safety check fails | `connector_revoke_safety_check_failed provider=<p> site=<s> error_class=<C>` | as above |
| `removal`, stored credential could not be decrypted | `removal_revoke_credential_unavailable error_class=<C>` | `connector_account.disabled` (`member_removed`) |
| `remediation`, stored credential could not be decrypted | `remediation_revoke_credential_unavailable error_class=<C>` (script output; the process is not scraped) | `connector_account.disabled` (`operator_remediation`) |
| `adapter_callback` (the adapter rejected a Google account after token exchange) | `gmail_revoke_failed: reason=<reason> status=<status>` (plus `gmail_revoke_post_failed: error_class=<C>` on a transport failure); no `connector_revoke_failed` line | **none** |

No log line carries an email or account id. When the audit event is missing, identify the mailbox from the request time (the member who was connecting or reconnecting) and ask that member to check Google's third-party access list.

## What an alert means and what to do

| Alert | First action |
|---|---|
| `EccConnectorRevokeFailed` / `EccConnectorRevokeErrorRatioHigh` | **Treat it as real by default.** Follow "Google revoke failed" in [`PHASE-10-GMAIL-RECOVERY.md`](../runbooks/PHASE-10-GMAIL-RECOVERY.md): find the event with the [revoke-error evidence table](#revoke-error-evidence) below, identify the connector or mailbox, and ask the mailbox owner to remove the app's access at Google. During R2 to R7 a real occurrence resets the two-week clean window. Only if **no** log line from that table exists for the alert's provider and site on any API process in the window (60 minutes before the alert to its end) treat it as a false positive. With the counters pre-initialised, `increase()` does not fire without a counted event, so look for a monitoring cause (for example a relabelling that renamed series) and record the empty log search as evidence. |
| `EccGmailRefreshInvalidGrantAboveBaseline` | Find the sync runs that failed with `invalid_grant` at that time and ask their mailbox owners whether they removed the app's access at Google (the most common cause of a single `invalid_grant`), and check whether an ECC revoke (`site` `callback_duplicate`, `reconnect_replaced`, `disable`) touched the same Google account shortly before. If an owner confirms they removed access themselves and no ECC revoke matches, record it as benign. If an ECC revoke matches under `ECC_GMAIL_REVOKE_SCOPE=none`, the revoke of one token killed a grant another live row uses: set the scope back to `global`, restart every process, and record it in the D2 record. |
| `EccPersonalDataShareRefusedSpike` | Expected in small numbers after R5, while people discover that email-derived rows cannot be shared. A spike from one path (for example `transfer`) usually means a workflow, such as removal preparation, is pushing admins to transfer. Check the `personal_data.share_refused` audit events. |
| `EccGmailIdentityMismatchRefused` | DS1 (no aliases) is working as designed. Tell the member to connect the Google account whose email matches their ECC account. A burst may mean a member is probing other people's mailboxes. |
| `EccGmailOwnerConflictRefused` / `EccPersonalConnectorAccessDenied` | Check whether this was legitimate confusion (shared mailbox) or probing. The audit event names the actor. |

`ecc:connector_revoke_skipped_unsafe:increase1d` has no alert. Review it after D2: under `global` it counts the extra Google grants users keep. If D2 proves per-token revocation and the scope becomes `none`, it should drop to the `adapter_callback` failure case only.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.4.0 | 2026-10-01 | The Spec A counters are pre-initialised at 0 at process start (#355), so the new-series terms are removed and every rule is plain `increase()`. Kept a note on why they existed and the remaining gaps (an event before a restarted process's first scrape that brings it back to the old value; lazily created engineering-provider series). The promtool test now starts the series at 0. Identity-mismatch, owner-conflict and access-denied alerts now stay firing for a day, not about 5 minutes | Lucky Jain |
| 1.3.0 | 2026-10-01 | PR review: revoke errors are real by default; new "Revoke-error evidence" table lists every log line (and audit event, if any) per site, and a false positive needs an empty log search; a failed safety check counts `error` except at `adapter_callback` and `removal`; owner-conflict refusals are `skipped_unsafe` only while the other row is live; canary first action confirms user-side removal before recording it benign | Lucky Jain |
| 1.2.0 | 2026-10-01 | Review fix (also: a fresh TSDB or replaced Prometheus server listed as a one-off false-positive cause): the new-series term is now `X unless last_over_time(X[55m] offset 5m)` (range selectors skip staleness markers and gaps), so a scrape gap or stale marker no longer re-fires existing series; a new series fires for about 5 minutes; remaining false positives (gap over 55 minutes, relabelling) documented; promtool test gains a stale/gap case | Lucky Jain |
| 1.1.0 | 2026-10-01 | Review fix: counters create each label set lazily at 1, so `increase()` alone misses the first event after every restart. Revoke-failed, canary, identity-mismatch, owner-conflict and access-denied rules gain a new-series term (`X unless X offset 55m`). Documented the remaining blind spot until counters are pre-initialised, and added a promtool unit-test snippet | Lucky Jain |
| 1.0.0 | 2026-10-01 | First alert rules for the Spec A counters: truthful revoke failures, the corrected refresh canary (all buckets above baseline), personal-data share-refused spikes, identity-mismatch / owner-conflict / access-denied notices and two recording rules | Lucky Jain |
