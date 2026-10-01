---
id: SPEC-A-ALERTS
title: Security Remediation Spec A Alert Rules
status: Active
version: 1.2.0
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
- **A label set does not exist until its first event, and then appears at 1.** `_Counter.inc` creates each label combination lazily, and the text format carries no created timestamp. `increase()` only measures the change *between* samples of a series, so it never sees the event that created the series: a series born at 1 that stays at 1 has an increase of 0. These counters are rare-event counters, and every R-step restart wipes them, so in practice most "first event after a restart" cases would be invisible to `increase()` alone. `increase()` does handle a restart when the label set already existed in the previous process and its last sample is still inside the window (the value drops, which counts as a reset).
- So every rare-event rule below is `increase(...) > 0` **or a new-series term**, `X unless last_over_time(X[55m] offset 5m)`: the current value of every series (per scrape target, since `instance` is part of the match) that has a sample now but **no sample at all between 60 and 5 minutes ago**. Such a series exists only because something was counted, so its value is the number of events since it appeared.
  - **Fire / resolve.** A new series matches from its first scrape until its first sample is more than 5 minutes old, so the new-series branch fires for **about 5 minutes** (one evaluation is enough; the rules have no `for:`) and then resolves. Alertmanager still delivers that one firing notification. After that, only `increase()` can fire for that series.
  - **Why this form.** A range selector (`[55m]`) skips staleness markers and tolerates gaps, so a scrape gap, a target briefly down, or a stale marker on an existing series does **not** make it look new again: as long as any sample of it exists in the 55-minute window, it is suppressed. The plain `X unless X offset 55m` used in 1.1.0 looked at a single instant and re-fired every existing non-zero series about 55 minutes after any such blip.
  - **Coverage.** The window (60 to 5 minutes ago) lies inside the 1-hour `increase()` window, so an old sample that suppresses the new-series branch is also seen by `increase()`, which catches a restart whose count dropped (a reset).
  - **Remaining false positives.** A series with **no** sample for more than 55 minutes that comes back unchanged (a target down for over an hour, then back without a restart), a relabelling that renames existing series, or a fresh TSDB / replaced Prometheus server (no history, so every existing non-zero series looks new on its first scrape) fires once for about 5 minutes. Before treating such an alert as real (for example before resetting the R7 clean window), confirm it against the audit events and the log lines named in "What an alert means and what to do".
- **Remaining blind spot, until the counters are pre-initialised.** If the previous process counted the same label set within the last hour and the new process reaches the *same* value, there is neither a drop nor a new series, so nothing fires. Pre-initialising every bounded label set to 0 at startup (tracked as a separate code follow-up) removes the blind spot and the need for the new-series terms; once it ships, plain `increase()` is correct. Until then, also check the change record's ops-script counts and the audit log after each restart. Never alert on a raw counter value.
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
- **`skipped_unsafe` has two meanings.** Usually `revoke_is_safe` found another live connection to the same Google account (expected under the default `ECC_GMAIL_REVOKE_SCOPE=global`). At `site="adapter_callback"` it also counts a revoke-safety check that itself failed (a database error), which fails closed and is not counted as `error`. Read an `adapter_callback` `skipped_unsafe` rise as "maybe DB trouble" too, and check the `gmail_revoke_on_reject_check_failed` log line.
- **Every Gmail callback refusal also revokes at `site="callback_failure"`.** That covers `identity_mismatch`, `owned_by_another_member`, `membership_inactive` and `insufficient_role`. The minted grant is revoked iff safe; under `global` an owner-conflict refusal is always `skipped_unsafe`, because the other member's row is live. So `callback_failure` volume tracks refusals. Net the refusals out before treating a `callback_failure` trend as an adapter problem (recording rule below).
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
        expr: |
          (sum by (provider, site) (increase(ecc_connector_revoke_total{result="error"}[1h])) > 0)
          or
          (sum by (provider, site) (
             ecc_connector_revoke_total{result="error"}
               unless last_over_time(ecc_connector_revoke_total{result="error"}[55m] offset 5m)
           ) > 0)
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.provider }} revoke failed at site {{ $labels.site }}; the provider grant may still be live"
          runbook: "docs/runbooks/PHASE-10-GMAIL-RECOVERY.md#google-revoke-failed-ecc_connector_revoke_totalprovidergmailresulterror"

      # The spec's ratio rule: more than 20% of attempted revokes failed in a day.
      # Denominator = attempted revokes (ok + error); skipped_unsafe were never attempted.
      # increase() misses each series' first event (see "Where the numbers come
      # from"), so this ratio undercounts after restarts; EccConnectorRevokeFailed
      # is the rule that catches single failures.
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
      # with a zero baseline, any invalid_grant fires). Second branch: any
      # invalid_grant series that appeared in the last ~5 minutes (its first
      # event, invisible to increase()) fires regardless of the baseline -- after
      # a restart every first invalid_grant is such a series, which is acceptable
      # for a canary.
      - alert: EccGmailRefreshInvalidGrantAboveBaseline
        expr: |
          (
            sum(increase(ecc_gmail_refresh_rejected_total{error="invalid_grant"}[1d])) > 0
            and
            sum(increase(ecc_gmail_refresh_rejected_total{error="invalid_grant"}[1d]))
              > 2 * ((sum(increase(ecc_gmail_refresh_rejected_total{error="invalid_grant"}[7d] offset 1d)) / 7) or vector(0))
          )
          or
          (sum(
             ecc_gmail_refresh_rejected_total{error="invalid_grant"}
               unless last_over_time(ecc_gmail_refresh_rejected_total{error="invalid_grant"}[55m] offset 5m)
           ) > 0)
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
        expr: |
          (sum(increase(ecc_connector_enrollment_refused_total{provider="gmail",reason="identity_mismatch"}[1d])) > 0)
          or
          (sum(
             ecc_connector_enrollment_refused_total{provider="gmail",reason="identity_mismatch"}
               unless last_over_time(ecc_connector_enrollment_refused_total{provider="gmail",reason="identity_mismatch"}[55m] offset 5m)
           ) > 0)
        labels:
          severity: info
        annotations:
          summary: "A Gmail connect was refused because the Google account differs from the member's own email"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#monitoring"

      # Someone tried to connect a mailbox another member already owns.
      - alert: EccGmailOwnerConflictRefused
        expr: |
          (sum(increase(ecc_connector_enrollment_refused_total{provider="gmail",reason="owned_by_another_member"}[1d])) > 0)
          or
          (sum(
             ecc_connector_enrollment_refused_total{provider="gmail",reason="owned_by_another_member"}
               unless last_over_time(ecc_connector_enrollment_refused_total{provider="gmail",reason="owned_by_another_member"}[55m] offset 5m)
           ) > 0)
        labels:
          severity: info
        annotations:
          summary: "A Gmail connect was refused: the mailbox is already connected by another member"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#monitoring"

      # A non-owner tried to sync or disable another member's Gmail connector (flag on).
      - alert: EccPersonalConnectorAccessDenied
        expr: |
          (sum by (provider, route) (increase(ecc_connector_access_denied_total[1d])) > 0)
          or
          (sum by (provider, route) (
             ecc_connector_access_denied_total unless last_over_time(ecc_connector_access_denied_total[55m] offset 5m)
           ) > 0)
        labels:
          severity: info
        annotations:
          summary: "Non-owner {{ $labels.route }} of a personal {{ $labels.provider }} connector was refused"
          runbook: "docs/runbooks/SPEC-A-ROLLOUT.md#monitoring"
```

The share-refused spike rule has no new-series term on purpose: it needs more than 5 events in an hour, so it only misses the first event of each series.

Thresholds (5 an hour, 3x, 2x, 20%) are starting points for a small internal deployment. Tune them after the first clean week, and record the change here.

## Rule unit test (promtool)

Save the rules block above as `spec-a-alerts.rules.yml` and this as `spec-a-alerts.test.yml`, then run `promtool test rules spec-a-alerts.test.yml` wherever promtool is installed (it is not part of this repository's toolchain). The first case is the one that matters: a series that appears at 1 and never changes, which `increase()` alone never alerts on; it fires for about 5 minutes. The second is a restart with a lower count, which `increase()` catches as a reset. The third is an existing series with a staleness marker and a 10-minute gap, which must not fire.

```yaml
rule_files:
  - spec-a-alerts.rules.yml
evaluation_interval: 1m
tests:
  # A revoke error in a fresh process: the series is absent for 90 minutes,
  # then appears at 1 (first sample at 90m) and stays there.
  - interval: 1m
    input_series:
      - series: 'ecc_connector_revoke_total{provider="gmail",site="removal",result="error",instance="api-1"}'
        values: '_x90 1x120'
    alert_rule_test:
      - eval_time: 80m          # before the event: nothing
        alertname: EccConnectorRevokeFailed
        exp_alerts: []
      - eval_time: 92m          # first event: fires through the new-series term
        alertname: EccConnectorRevokeFailed
        exp_alerts:
          - exp_labels: {severity: warning, provider: gmail, site: removal}
            exp_annotations:
              summary: "gmail revoke failed at site removal; the provider grant may still be live"
              runbook: "docs/runbooks/PHASE-10-GMAIL-RECOVERY.md#google-revoke-failed-ecc_connector_revoke_totalprovidergmailresulterror"
      - eval_time: 100m         # the 90m sample is now inside the 60-5 min window: resolved
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
  # same value again: no new event, so no alert (the 1.1.0 instant-offset term
  # fired here).
  - interval: 1m
    input_series:
      - series: 'ecc_connector_revoke_total{provider="gmail",site="disable",result="error",instance="api-1"}'
        values: '1x30 stale _x9 1x60'
    alert_rule_test:
      - eval_time: 45m          # back after the gap: earlier samples are in the window
        alertname: EccConnectorRevokeFailed
        exp_alerts: []
      - eval_time: 90m
        alertname: EccConnectorRevokeFailed
        exp_alerts: []
```

This test has not been run here: promtool is not available in this environment. Run it before loading the rules.

## What an alert means and what to do

| Alert | First action |
|---|---|
| `EccConnectorRevokeFailed` / `EccConnectorRevokeErrorRatioHigh` | Follow "Google revoke failed" in [`PHASE-10-GMAIL-RECOVERY.md`](../runbooks/PHASE-10-GMAIL-RECOVERY.md). Find the connector from the disconnect, removal or purge audit event at that time, and ask the mailbox owner to remove the app's access at Google. During R2 to R7 any occurrence resets the two-week clean window. |
| `EccGmailRefreshInvalidGrantAboveBaseline` | Under `ECC_GMAIL_REVOKE_SCOPE=none`, the revoke of one token may have killed a grant another live row uses. Set the scope back to `global` and restart every process, then record it in the D2 record. Under `global`, check for users revoking access at Google themselves. |
| `EccPersonalDataShareRefusedSpike` | Expected in small numbers after R5, while people discover that email-derived rows cannot be shared. A spike from one path (for example `transfer`) usually means a workflow, such as removal preparation, is pushing admins to transfer. Check the `personal_data.share_refused` audit events. |
| `EccGmailIdentityMismatchRefused` | DS1 (no aliases) is working as designed. Tell the member to connect the Google account whose email matches their ECC account. A burst may mean a member is probing other people's mailboxes. |
| `EccGmailOwnerConflictRefused` / `EccPersonalConnectorAccessDenied` | Check whether this was legitimate confusion (shared mailbox) or probing. The audit event names the actor. |

`ecc:connector_revoke_skipped_unsafe:increase1d` has no alert. Review it after D2: under `global` it counts the extra Google grants users keep. If D2 proves per-token revocation and the scope becomes `none`, it should drop to the `adapter_callback` failure case only.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.2.0 | 2026-10-01 | Review fix (also: a fresh TSDB or replaced Prometheus server listed as a one-off false-positive cause): the new-series term is now `X unless last_over_time(X[55m] offset 5m)` (range selectors skip staleness markers and gaps), so a scrape gap or stale marker no longer re-fires existing series; a new series fires for about 5 minutes; remaining false positives (gap over 55 minutes, relabelling) documented; promtool test gains a stale/gap case | Lucky Jain |
| 1.1.0 | 2026-10-01 | Review fix: counters create each label set lazily at 1, so `increase()` alone misses the first event after every restart. Revoke-failed, canary, identity-mismatch, owner-conflict and access-denied rules gain a new-series term (`X unless X offset 55m`). Documented the remaining blind spot until counters are pre-initialised, and added a promtool unit-test snippet | Lucky Jain |
| 1.0.0 | 2026-10-01 | First alert rules for the Spec A counters: truthful revoke failures, the corrected refresh canary (all buckets above baseline), personal-data share-refused spikes, identity-mismatch / owner-conflict / access-denied notices and two recording rules | Lucky Jain |
