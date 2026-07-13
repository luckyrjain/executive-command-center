---
id: STD-001
title: Repository Standards
status: Draft
version: 1.0.0
owner: Lucky Jain
reviewers:
  - Lucky Jain
type: Engineering Standard
depends_on:
  - RFC-002
  - RFC-003
  - RFC-004
  - RFC-005
---

# STD-001 — Repository Standards

---

# Executive Summary

This standard defines how software is built inside the Executive Command Center repository.

Unlike RFCs, which define architecture and intent, this document defines implementation rules.

Every engineer.

Every AI coding agent.

Every pull request.

Must comply with these standards.

Violation of these standards is considered a defect.

---

# Guiding Principles

Repository standards exist to ensure the codebase remains:

- Understandable
- Predictable
- Testable
- Reviewable
- AI-friendly
- Long-lived

Optimization for short-term development speed is explicitly rejected if it harms long-term maintainability.

---

# Repository Structure

The repository SHALL follow this layout.

```
executive-command-center/

docs/

specification/

adr/

standards/

phases/

backend/

domains/

platform/

shared/

frontend/

components/

features/

layouts/

hooks/

design-system/

packages/

scripts/

docker/

infrastructure/

tests/

integration/

e2e/

fixtures/

.github/

```

No additional top-level directories without an ADR.

---

# Domain Structure

Every backend domain SHALL follow the same structure.

```
domain/

application/

domain/

infrastructure/

contracts/

api/

workers/

tests/

README.md

```

Consistency is mandatory.

---

# Frontend Structure

```
feature/

components/

pages/

hooks/

services/

state/

tests/

README.md
```

Features own UI.

Components remain reusable.

---

# Naming Standards

## Files

snake_case

Examples

```
meeting_service.py

knowledge_graph.py

attention_engine.py
```

---

## Python Classes

PascalCase

```
MeetingPlanner

KnowledgeGraph

PromptValidator
```

---

## Functions

snake_case

```
calculate_priority()

load_context()

resolve_identity()
```

---

## React Components

PascalCase

```
ExecutiveDashboard.tsx

MeetingCard.tsx

RiskWidget.tsx
```

---

## Variables

Descriptive.

Avoid abbreviations.

Bad

```
ctx

res

obj

tmp
```

Good

```
meeting_context

knowledge_result

priority_score
```

---

# Module Size

Maximum file size

300 lines.

Hard limit

500 lines.

Anything larger requires justification.

---

# Function Size

Target

20 lines.

Maximum

50 lines.

Longer functions should be decomposed.

---

# Function Responsibilities

One function.

One responsibility.

Never combine

Validation

Business logic

Persistence

Logging

AI

inside one function.

---

# Class Responsibilities

Classes own behaviour.

Not utility collections.

God Objects prohibited.

---

# Dependency Direction

Allowed.

```
Application

↓

Domain

↓

Infrastructure
```

Forbidden.

```
Infrastructure

↓

Application
```

Circular dependencies prohibited.

---

# Import Rules

No wildcard imports.

Explicit imports only.

Imports grouped.

1 Standard Library

2 Third-party

3 Internal

---

# Documentation

Every public class requires

Purpose

Responsibilities

Dependencies

Examples (where appropriate)

Public APIs require docstrings.

---

# Comments

Comments explain

Why.

Never

What.

Bad

```
Increment i by one
```

Good

```
Retry because GitHub occasionally returns stale ETags.
```

---

# Logging

Structured logging only.

Required fields.

```
timestamp

service

domain

correlation_id

request_id

user_id

level

message
```

Never log

Secrets

Passwords

Tokens

PII

Prompt context

---

# Error Handling

Never swallow exceptions.

Every exception must be

Handled

Translated

Logged

or

Propagated.

---

# Configuration

No hardcoded values.

Everything configurable.

Configuration hierarchy.

```
Default

↓

Environment

↓

Workspace

↓

User
```

---

# Testing Standards

Every feature requires.

Unit Tests

Integration Tests

Contract Tests

Acceptance Tests

AI Tests (if applicable)

---

# Coverage

Minimum

80%

Critical domains

95%

Coverage is measured.

Not assumed.

---

# AI Coding Standards

AI-generated code is treated exactly like human code.

Additional requirements.

Every generated file references FR IDs.

Every prompt version committed.

No AI-generated TODOs.

No placeholder implementations.

No commented-out code.

---

# Requirement Traceability

Every implementation references.

FR IDs

Acceptance Tests

ADR (if applicable)

Example

```
Implements:

FR-P1-012

AT-P1-019
```

---

# Commit Messages

Format.

```
type(scope): summary

```

Examples.

```
feat(planner): implement focus scheduling

fix(memory): resolve graph traversal bug

docs(rfc004): update AI runtime

refactor(pkos): simplify retrieval pipeline
```

---

# Pull Request Standards

Every PR includes.

Problem

Solution

FR IDs

Test Evidence

Screenshots (if UI)

Architecture Impact

Breaking Changes

Rollback Strategy

---

# Code Review Checklist

Reviewers verify.

Correctness

Architecture

Performance

Security

Documentation

Tests

Naming

Logging

Observability

Traceability

---

# AI Review Checklist

Additional review.

Prompt updated?

Evidence preserved?

Model Router used?

Schemas validated?

Reflection required?

Tool permissions correct?

Hallucination risk assessed?

---

# Documentation Rule

Behaviour changes require

Code

Tests

Specification

Documentation

in the same PR.

Specification drift is prohibited.

---

# Feature Development Workflow

```
RFC

↓

Specification

↓

ADR (if required)

↓

Implementation

↓

Tests

↓

Documentation

↓

Review

↓

Merge
```

Implementation never starts from an idea.

---

# Stop-and-Ask Protocol

If any requirement is

Ambiguous

Missing

Contradictory

Incomplete

The developer MUST stop.

Required action.

Create

Specification Change Request

Never guess.

---

# Definition of Done

A feature is complete only if.

Code merged.

Tests passing.

Documentation updated.

Architecture compliant.

Security reviewed.

Observability added.

Metrics exposed.

Runbook updated (if operational).

No TODOs remain.

---

# Architecture Enforcement

CI SHALL automatically verify.

Dependency direction.

Circular dependencies.

File size.

Test coverage.

Lint.

Formatting.

Specification references.

Forbidden imports.

Approved technologies.

---

# Forbidden Practices

The following are prohibited.

Hardcoded secrets.

Business logic in UI.

Business logic in prompts.

Direct database access across domains.

Circular dependencies.

Global mutable state.

Copy-paste implementations.

Magic numbers.

Silent failures.

Unchecked exceptions.

Latest dependency versions.

---

# AI-Specific Rules

AI coding agents SHALL NOT.

Invent APIs.

Invent database schema.

Invent technologies.

Skip tests.

Skip documentation.

Ignore architecture.

Guess ambiguous requirements.

Modify specifications implicitly.

---

# Repository Health Metrics

Measured continuously.

Build Success

Test Coverage

Dependency Drift

Documentation Coverage

Architecture Violations

Technical Debt

Cyclomatic Complexity

Code Duplication

Average Review Time

---

# Technical Debt

Every intentional shortcut requires.

Reason.

Owner.

Removal Date.

Risk.

ADR (if significant).

Invisible technical debt is prohibited.

---

# Security Standards

Every feature must include.

Authorization.

Audit.

Input Validation.

Structured Logging.

Threat Review.

Secret Handling.

Prompt Injection Review (AI features).

---

# Performance Standards

Dashboard

<500ms

API

<200ms

Search

<300ms

Knowledge Lookup

<250ms

Performance regressions block merges.

---

# Repository Fitness Functions

AFF-STD-001

No architecture violations.

---

AFF-STD-002

No circular dependencies.

---

AFF-STD-003

100% specification traceability.

---

AFF-STD-004

No unapproved technologies.

---

AFF-STD-005

No TODOs on main.

---

AFF-STD-006

No failing tests.

---

AFF-STD-007

No undocumented public APIs.

---

AFF-STD-008

No production secrets committed.

---

AFF-STD-009

Every merge reproducible.

---

AFF-STD-010

Repository bootstraps successfully from scratch.

---

# Repository Oath

Every contributor agrees to leave the repository in a better state than they found it.

Before merging ask:

- Is it simpler?
- Is it clearer?
- Is it more maintainable?
- Is it better documented?
- Does it align with the specification?

If the answer is no,

the change is not ready.

---

# Summary

STD-001 is the operational constitution of the Executive Command Center repository.

It ensures that every contribution—whether written by a human or generated by AI—is consistent with the architectural vision, technology registry, engineering philosophy, and long-term maintainability goals of the platform.

This document is intentionally prescriptive.

Consistency is considered a competitive advantage.

---

# Changelog

## Version 1.0.0

Initial repository standard covering:

- Repository layout
- Naming conventions
- Module structure
- Coding standards
- AI coding rules
- Testing
- Documentation
- Reviews
- Traceability
- CI enforcement
- Performance
- Security
- Definition of Done

---

**Status:** Draft

**Estimated Review Time:** 30–40 minutes

**Next Document:** RFC-000 — Document Control
