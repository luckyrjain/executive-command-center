---
id: PRODUCT-KPI-CONTRACT
title: Product KPI Contract
status: Implemented
version: 1.0.0
owner: Lucky Jain
updated: 2026-08-06
---

# Product KPI Contract

This contract makes every success metric named by [`../RFC-001.md#success-metrics`](../RFC-001.md#success-metrics) measurable without pretending telemetry exists. ECC has no approved product-analytics event pipeline. `not_collectable` means no trustworthy source currently exists; it is unknown, never zero. Manual dogfood records are the only current live product-use measurement source and must not be backfilled by an agent or automated test; versioned offline test/evaluation fixtures remain valid non-production sources where named below.

Privacy classes are `operational` (non-content service facts), `behavioral` (user interaction/activity), and `sensitive-derived` (quality or inference computed from user content). No person/employee scoring or cross-user ranking is allowed.

## User experience

| Metric | Decision and formula/duration | Population and window | Source / collection state | Privacy | Target | Owner | Cadence |
|---|---|---|---|---|---|---|---|
| Daily active usage | **Retain as a manual gate proxy.** Distinct consenting dogfood users completing one named core workflow per local calendar day. | Dogfood users; daily | Phase 1 daily-use record / manual | Behavioral | 7 real usage days for the Phase 1 gate | Lucky Jain | Daily during gate |
| Weekly active usage | **Retain; establish a baseline before setting a target.** Distinct consenting dogfood users completing one named core workflow in a rolling 7-day window. | Dogfood users; rolling 7 days | Manual records / not_collectable until user IDs and consented event source exist | Behavioral | baseline_required | Lucky Jain | Weekly |
| Morning briefing completion | **Retain; establish a baseline.** Completed brief reviews divided by brief reviews started; completion means reaching the end and explicitly closing/acknowledging. | Consenting users; weekly | No completion event / not_collectable | Behavioral | baseline_required | Lucky Jain | Weekly |
| Meeting preparation usage | **Retain; establish a baseline.** Meetings with an opened prep pack divided by eligible meetings beginning in the window. | Consenting users with eligible meetings; weekly | No approved analytics join / not_collectable | Behavioral | baseline_required | Lucky Jain | Weekly |
| Average planning time | **Retain; establish a baseline.** Median elapsed time from deliberate planning start to accepted/closed plan; exclude idle intervals after 30 minutes. | Consenting planning sessions; weekly | No session instrumentation / not_collectable | Behavioral | baseline_required | Lucky Jain | Weekly |
| Inbox zero time | **Defer reporting until the inbox population is defined.** Median active elapsed time to move the defined actionable inbox from non-zero to zero. | Consenting users with a non-zero inbox; weekly | Inbox/product event undefined / not_collectable | Behavioral | baseline_required | Lucky Jain | Weekly |
| Context switches reduced | **Retain as an opt-in manual outcome; prohibit OS surveillance.** Per-user change in externally self-reported application switches per comparable workday versus pre-ECC baseline. | Consenting dogfood users; paired baseline and 2-week window | No OS surveillance; manual diary only / not_collectable | Behavioral | baseline_required | Lucky Jain | Per dogfood window |

## AI and recommendation quality

| Metric | Decision and formula/duration | Population and window | Source / collection state | Privacy | Target | Owner | Cadence |
|---|---|---|---|---|---|---|---|
| Task extraction accuracy | **Retain for a future labelled evaluation; do not report from production.** Correct extracted task fields divided by all predicted and missed gold task fields; publish precision/recall beside the aggregate. | Frozen consented/redacted evaluation set; model/prompt version | No production extraction evaluation source / not_collectable | Sensitive-derived | baseline_required | Lucky Jain | Per model/prompt release |
| Commitment extraction accuracy | **Retain for a future labelled evaluation; do not report from production.** Correct commitment fields divided by predicted and missed gold commitment fields; publish precision/recall. | Frozen consented/redacted evaluation set; model/prompt version | No production extraction evaluation source / not_collectable | Sensitive-derived | baseline_required | Lucky Jain | Per model/prompt release |
| Meeting preparation quality | **Retain as an offline human evaluation.** Human rubric points earned divided by available points, with unsupported facts separately counted. | Frozen meeting-prep evaluation set; model/prompt version | Phase 4 evaluation artifacts / collectable only for the approved fixture task | Sensitive-derived | baseline_required for the human rubric; automated safety floors are separate | Lucky Jain | Per model/prompt release |
| Recommendation acceptance rate | **Retain; establish a product baseline.** Accepted recommendations divided by recommendations explicitly decided; exclude expired/unseen. | Consenting dogfood users; 2-week window and policy version | Recommendation decisions / derivable only after approved aggregate query | Behavioral | baseline_required | Lucky Jain | Per dogfood window |
| False recommendation rate | **Retain; freeze the feedback taxonomy before collection.** Recommendations explicitly labelled incorrect or unsafe divided by explicitly evaluated recommendations. | Consenting dogfood users; 2-week window and policy version | No frozen feedback taxonomy/query / not_collectable | Sensitive-derived | baseline_required | Lucky Jain | Per dogfood window |
| Hallucination rate | **Retain as a release-blocking offline safety metric.** Outputs with one or more unsupported factual claims divided by human-reviewed outputs. | Frozen evaluated outputs; model/prompt/tool version | Phase 4 evaluation only where rubric exists | Sensitive-derived | 0 prohibited-fact occurrences per evaluated task release | Lucky Jain | Per model/prompt release |
| Memory retrieval precision | **Retain as an offline retrieval gate.** Relevant retrieved items divided by retrieved items at the frozen cutoff. | Frozen retrieval queries; index/version | Retrieval benchmark fixtures / collectable in benchmark only | Sensitive-derived | precision@5 ≥ 0.14 | Lucky Jain | Per retrieval release |
| Memory retrieval recall | **Retain as an offline retrieval gate.** Relevant retrieved items divided by all labelled relevant items at the frozen cutoff. | Frozen retrieval queries; index/version | Retrieval benchmark fixtures / collectable in benchmark only | Sensitive-derived | recall@10 ≥ 0.70 | Lucky Jain | Per retrieval release |

## Performance

| Metric | Decision and formula/duration | Population and window | Source / collection state | Privacy | Target | Owner | Cadence |
|---|---|---|---|---|---|---|---|
| Dashboard load time | **Retain as a release gate.** p50/p95 navigation start to usable primary dashboard under a frozen representative dataset and device profile. | Release candidate; fixed benchmark run | Phase 1 performance test / test-only | Operational | p95 ≤ 2 seconds (RFC-001 NFR-002) | Lucky Jain | Per release |
| Synchronization latency | **Retain per provider; establish a real-account baseline.** p50/p95 source-change time to committed local projection time. | Successful real-account sync items; 7 days | Source event time is incomplete/provider-specific / not_collectable | Operational | baseline_required | Lucky Jain | Per connector validation |
| Local search latency | **Retain as a release gate.** p50/p95 request start to complete local result response under a frozen corpus. | Release candidate; fixed benchmark run | Existing test evidence where exercised / test-only | Operational | p95 < 500 ms local and < 800 ms CI | Lucky Jain | Per release |
| AI response latency | **Retain as an offline task/model release gate.** p50/p95 request start to validated response or deterministic fallback, by task/model. | Frozen evaluation calls; model/task version | Phase 4 evaluation artifacts / fixture-only | Operational | p95 < 20 s for `attention.explain_item`; < 35 s for `meeting.prep_summary`; baseline_required for other tasks | Lucky Jain | Per model release |
| Memory retrieval latency | **Retain as a release gate by retrieval mode.** p50/p95 query start to ranked result set under a frozen corpus. | Release candidate; benchmark query set | Phase 2 retrieval benchmark / test-only | Operational | lexical p95 < 500 ms; hybrid p95 < 800 ms | Lucky Jain | Per retrieval release |

## Product outcomes

| Metric | Decision and formula/duration | Population and window | Source / collection state | Privacy | Target | Owner | Cadence |
|---|---|---|---|---|---|---|---|
| Missed commitments reduced | **Retain as a manual paired-window outcome.** Per-user change in confirmed missed critical commitments per comparable 2-week window versus pre-ECC baseline. | Consenting dogfood users; paired windows | Manual validation only / not_collectable without baseline | Sensitive-derived | Phase 3: zero missed critical items during its 2-week gate | Lucky Jain | Per dogfood window |
| Follow-up completion rate | **Retain; establish a baseline.** Follow-ups completed by due date divided by follow-ups due in the window; exclude cancelled items. | Consenting dogfood users; weekly | No approved aggregate query / not_collectable | Behavioral | baseline_required | Lucky Jain | Weekly |
| Decision retrieval success | **Retain as a human-judged outcome; establish a baseline.** Successful human-judged retrievals divided by attempted named-decision retrievals. | Consenting dogfood users; 2-week window | No explicit outcome event / not_collectable | Sensitive-derived | baseline_required | Lucky Jain | Per dogfood window |
| User trust score | **Retain only as an opt-in aggregate survey.** Median response to a frozen 1–5 trust question after reviewing evidence/explanations. | Consenting dogfood users; phase gate | No survey instrument / not_collectable | Sensitive-derived | baseline_required | Lucky Jain | Per phase gate |
| Retention | **Retain only after an approved consented identity/event source exists.** Consenting users active in the evaluation window who return in the named later window; always state D7/D30 and denominator. | Dogfood cohort; D7 and D30 | No approved analytics identity/event source / not_collectable | Behavioral | baseline_required | Lucky Jain | Monthly when collectable |

## Existing manual gates are authoritative

This contract does not weaken existing thresholds: Phase 1 requires seven real usage days; Phase 3 requires zero missed critical items, at least 80% top-five usefulness, at least 60% plan acceptance, and under 10% false urgency across two weeks; Phase 5 requires fourteen staged days with zero unauthorized or simulation-caused effects plus its named stop/recovery exercises. The source records are [`../runbooks/PHASE-1-DAILY-USE.md`](../runbooks/PHASE-1-DAILY-USE.md), [`../runbooks/PHASE-3-DOGFOOD.md`](../runbooks/PHASE-3-DOGFOOD.md), and [`../runbooks/PHASE-5-DOGFOOD.md`](../runbooks/PHASE-5-DOGFOOD.md).

## Activation rule

Before changing any metric from `not_collectable`, approve its event/source contract, purpose, consent, retention, access, deletion behavior, aggregation threshold, and validation query. Missing data remains null/unknown. It must never be imputed as zero, copied from fixtures into a product claim, or collected merely because the application can technically emit it.
