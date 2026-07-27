---
id: PHASE-006-TEST-PLAN
title: Phase 6 Test Plan
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 6 Test Plan

**Task 1 status**: `tests/test_engineering_connectors_postgres.py` covers the sandbox adapter, connector account lifecycle, cursor durability across backfill/incremental sync, disconnect, and workspace isolation. Webhook dedupe, rate limits, access loss, deletion, rename, metric fixtures and ambiguous identities have no real provider or metric computation to test against yet -- each lands with the task that implements it (`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`, Tasks 2-6).

Test sandbox adapters, backfill/incremental sync, cursor durability, webhook dedupe, rate limits, access loss, deletion, rename, disconnect and rebuild. Validate metric fixtures and definitions against hand-calculated results. Verify partial coverage and ambiguous identities.

Security covers token redaction, least scopes, webhook signatures, malicious payloads, isolation and approved writes. Ethics checks prohibit person scores/leaderboards. Browser acceptance connects a sandbox, observes sync/coverage, traces a risk to evidence and handles degraded states.
