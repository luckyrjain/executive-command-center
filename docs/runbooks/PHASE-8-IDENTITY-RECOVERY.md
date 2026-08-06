---
id: PHASE-8-IDENTITY-RECOVERY
title: Phase 8 Identity Recovery Runbook
status: Active
version: 1.0.0
owner: Lucky Jain
updated: 2026-08-06
---

# Phase 8 Identity Recovery Runbook

Production first-owner provisioning, password reset/account recovery, and MFA or risk-based step-up are **Unsupported — production blockers**. They are tracked as PR-001, PR-002, and PR-003 in [`../operations/PRODUCTION-READINESS.md#blockers`](../operations/PRODUCTION-READINESS.md#blockers).

## Current safe behavior

- For local development, use `uv run python scripts/bootstrap_dev.py` only when `ECC_ENV=development` and the database is local. The command refuses unsafe environments by default and its one-time URL expires.
- Production registration/login code is not an approved first-owner or recovery ceremony. Do not claim production readiness from successful local login tests.
- If a production-like account cannot authenticate, preserve the account, stop the rollout, and collect non-secret request IDs and audit events. Do not change password hashes, ownership, memberships, sessions, or invitation rows directly.
- For a suspected compromise, contain access at the deployment/network layer and preserve audit evidence. There is no approved user-facing recovery plus factor-verification workflow.

## Unsupported procedures

There is no supported database command, bootstrap override, administrator impersonation, manual session insertion, password-hash replacement, or ownership-row edit for recovery. There is no supported MFA enrollment, challenge, recovery code, or step-up operation. These omissions are deliberate blockers, not invitations to improvise.

## Evidence required before use

The production identity runbook must eventually include an auditable first-owner ceremony, enumeration-resistant recovery, session revocation, throttling, factor enrollment/recovery, high-risk action step-up, negative authorization cases, clean-environment exercises, and independent review. Until that evidence closes PR-001–PR-003, Phase 8 remains unpromoted.
