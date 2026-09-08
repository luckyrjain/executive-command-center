---
id: CONTEXT-ATTENTION
title: Attention Context
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Attention

The ranked projection of what needs a human's attention right now, and the supporting machinery around it.

## Language

**AttentionItem**:
A scored, ranked pointer to another entity (a task, commitment, risk, waiting link, risk review, meeting, or
email thread) that needs a human's attention. It is a projection, not a proposal — see Recommendation in
[Governance](../governance/CONTEXT.md) for the concept that actually proposes an action.

**AttentionFeedback**:
A user's usefulness rating on an AttentionItem.
_Avoid_: conflating with RecommendationFeedback ([Governance](../governance/CONTEXT.md)) or the personal
domain's own feedback concept ([Personal](../personal/CONTEXT.md)) — three separate, unrelated feedback
mechanisms exist across this codebase under similar names.

**WaitingLink**:
A directional relationship — waiting on, blocked by, or delegated — between one entity and a counterparty.

**RiskReview**:
A periodic review event on a Risk, recording an outcome (no change, escalated, de-escalated, mitigated,
closed). The review event itself; the Risk register entry it reviews is owned by
[Governance](../governance/CONTEXT.md).
_Avoid_: using "closed" here interchangeably with a Risk's own terminal status — RiskReview.outcome and
Risk.status are two different state machines that happen to share a value

**PlanningConstraint**:
A hard or soft calendar reservation or deadline used when planning someone's time.

**CapacityProfile**:
A user's weekly available-minutes and focus-minutes template.

**MeetingPack**:
See [Scheduling](../scheduling/CONTEXT.md) — generated and owned here, despite being entirely about Meetings.
