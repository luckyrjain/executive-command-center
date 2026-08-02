---
id: PHASE-008-TEST-PLAN
title: Phase 8 Test Plan
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 8 Test Plan

Build a complete role/resource/action authorization matrix (the bounded `owner|admin|member|viewer` enum against every `resource_type`/action pair, per `docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` Decision 2 -- closed and enumerable specifically so this matrix is a literal finite test suite, not an open-ended fuzz target) plus property tests for deny-by-default. Test invitations, duplicate/expired tokens, workspace switching, private/shared scopes, grants, revocation, delegation acceptance, ownership transfer and member removal.

Adversarial tests cover IDOR, privilege escalation, confused deputy, stale caches, background-job authority and private-domain leakage -- including a dedicated test proving no `resource_grants` row can ever be created against a Phase 7 personal-domain `resource_type`, regardless of the requesting role. Revocation propagation is tested directly against the 60-second SLO (revoke a membership/grant, assert the very next request denies with no sleep; revoke mid-background-job, assert the job's next step denies) rather than merely asserted to work. Browser acceptance uses multiple identities for share/revoke/delegate/remove flows. Backup/restore preserves memberships, grants, ownership and audit.
