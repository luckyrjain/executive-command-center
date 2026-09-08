---
id: CONTEXT-MAP
title: Domain Context Map
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Context Map

Real bounded contexts in Executive Command Center's backend, one per `backend/ecc/domains/*` grouping,
verified against actual code (2026-09-08) — not against `docs/domain/DOMAIN-MODEL.md`'s conceptual ownership
map, which this audit found has substantial drift from what's actually implemented (see "Known conflicts"
below). Each context below has its own `CONTEXT.md` — a pure glossary, no implementation detail.

## Contexts

- [Identity & Access](./identity-access/CONTEXT.md) — accounts, workspace membership, roles, sessions, resource sharing and delegation
- [Planning](./planning/CONTEXT.md) — task capture and lifecycle
- [Calendar](./calendar/CONTEXT.md) — scheduled time intervals
- [Scheduling](./scheduling/CONTEXT.md) — meetings and meeting preparation
- [Knowledge](./knowledge/CONTEXT.md) — the entity graph: people, organizations, claims, relationships, evidence, notes
- [Attention](./attention/CONTEXT.md) — the ranked "what needs a human's attention" projection
- [Governance](./governance/CONTEXT.md) — the risk register and recommendations
- [Communication](./communication/CONTEXT.md) — commitments: promises tracked between people
- [Personal](./personal/CONTEXT.md) — a per-owner encrypted personal-data vault, plus a Gmail connector sharing the same package
- [AI Runtime](./ai-runtime/CONTEXT.md) — model routing, prompt/tool versioning, run execution, evaluation
- [Automation](./automation/CONTEXT.md) — durable workflow execution, approvals, kill switches
- [Engineering](./engineering/CONTEXT.md) — connector accounts and provider sync (GitHub/GitLab/Jira/Datadog/Gmail)

## Relationships

- **Identity & Access → everywhere**: every resource in every other context is Workspace- and owner-scoped through it.
- **Scheduling → Calendar**: a Meeting may link to zero or one CalendarEvent; when linked, its timing is derived read-only from that CalendarEvent.
- **Scheduling → Attention**: MeetingPack (meeting prep) is generated and owned by Attention, not Scheduling, despite being entirely about Meetings.
- **Attention ↔ Governance**: an AttentionItem can point at a Risk; RiskReview (the review event) is owned by Attention, but the Risk register entry itself is owned by Governance — the same word "Risk" names two layered but distinct lifecycles.
- **Knowledge ↔ Identity & Access**: Person and Organization are conceptually Identity's, but all persistence and business rules for them actually live in Knowledge's entity graph.
- **Communication and Personal are not one context**, despite sitting next to each other historically — Commitment (Communication) has no relationship to messaging or email at all.
- **Personal's Gmail Connector** sub-area is built on Engineering's connector framework, not Personal's own vault mechanism.
- **Automation ↔ Engineering**: an ActionAdapter (Automation, one write action) is a different concept from a ConnectorAdapter (Engineering, one provider's whole account lifecycle) — both called "adapter," never interchangeable. Already correctly disambiguated in code.
- **AI Runtime ↔ Automation**: RoutingPolicy (AI Runtime, model eligibility) and AutomationPolicy (Automation, execution authority) share only the English word "Policy."

## Known conflicts with `docs/domain/DOMAIN-MODEL.md`

`DOMAIN-MODEL.md` is marked Approved and "Phase 1 frozen," but a code-grounded audit (2026-09-08) found real
drift beyond what that document's own existing "backend package note" annotations already disclose:

- **Entities named as implemented that don't exist in code at all**: Project, Reminder, Brief, KnowledgeItem,
  Conversation, Message, SourceRecord.
- **Communication's ownership row is wrong**: it claims Conversation, Message, EmailThread, Commitment. Only
  Commitment is real and lives in `communication/`; EmailThread is real but lives in `personal/`.
- **Goal exists, but not as described**: the doc pairs it with Project as a business "measurable outcome"; the
  real `goals` table is a personal habit-tracking construct in `personal/habits.py`, unrelated to Task/Project.
- **"Planning" is not one owning package**: Task, CalendarEvent, Meeting, and Goal actually live in four
  separate real packages (`planning/`, `calendar/`, `scheduling/`, `personal/`).
- **Automation is missing from the ownership map entirely** — a substantial, independently-designed domain
  with no row at all.
- **SyncCursor** is listed as if a first-class entity; in code it's a bookkeeping table with no dedicated class.

This map and its per-context `CONTEXT.md` files describe what the code actually does today.
`DOMAIN-MODEL.md` remains the fuller spec (lifecycles, invariants, API shape) for the parts that do match
reality — **it has since been corrected in place** (2026-09-08) with inline notes at every stale claim,
preserving the original text rather than rewriting it, matching that document's own established correction
convention. Its "Phase 1 freeze" clause was never actually violated: every entity found unimplemented or
mismeant falls outside that clause's own narrower, explicitly-named frozen list.
