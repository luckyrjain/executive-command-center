---
id: EVIDENCE-POLICY
title: Durable Engineering Evidence Policy
status: Active
version: 1.0.0
owner: Lucky Jain
---

# Durable Engineering Evidence Policy

## Purpose

Engineering, validation, recovery, and promotion claims must be inspectable
after the authoring session ends. Evidence supports a claim; it does not
silently upgrade the phase state in [`status.json`](../phases/status.json).

## Accepted evidence

- committed test, benchmark, security-scan, or recovery reports;
- immutable commit or pull-request URLs;
- GitHub Actions workflow and job URLs tied to a commit;
- committed human validation records with date, operator, scope, and result;
- direct source and test references when the check is mechanically rerunnable.

Evidence records must name the tested commit, command or procedure, result,
and any environment limitation. Sensitive values, message bodies, tokens,
personal data, and secrets must be redacted.

## Not durable evidence

- uncommitted local agent reports or session transcripts;
- a branch name without an immutable commit;
- a checked box without a result record;
- a claim that a command passed without captured output or a rerunnable path;
- reconstructed review history based on memory.

When durable evidence is absent, label the claim `unverified`. Do not infer a
zero finding count, successful recovery, completed dogfood day, or promotion
decision from missing data.

## Gate ownership

Automated evidence may close only the automated check it executes. Human
dogfood, change review, production-readiness, and promotion gates require
their named human record. The canonical phase registry changes only after the
corresponding evidence is committed and reviewed.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Established durable repository evidence rules | Lucky Jain |
