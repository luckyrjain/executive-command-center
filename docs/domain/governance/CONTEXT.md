# Governance

The risk register, and proposed actions on domain state.

## Language

**Risk**:
A register entry: a probability, an impact, an owner, and a lifecycle from identified through assessed,
monitoring or mitigating, to materialized or closed. The register entry itself — see RiskReview in
[Attention](../attention/CONTEXT.md) for the periodic review event layered on top of it.

**Recommendation**:
An AI- or rule-proposed action to create or mutate a task, commitment, or risk. Never mutates domain state
directly on its own — it has its own lifecycle (proposed, then pending_confirmation, then accepted or
rejected/expired/superseded, then executed or failed) requiring an explicit confirmation step.

## Open question

DOMAIN-MODEL.md names RecommendationFeedback and UserFeedback as first-class entities. In code, only a
write-only `recommendation_feedback` table exists with no schema or response model of its own — it records
confirm/reject/defer/pin actions but isn't an exposed resource. Whether this should become a real, queryable
concept or stay an internal audit trail is an open product decision.
