---
id: PHASE-004-UX-STATES
title: Phase 4 AI UX States
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 4 UX States

AI-enhanced surfaces distinguish deterministic content from generated content and show source, freshness and confidence. Required states: AI disabled, local model unavailable (Ollama offline -- `chapter-02b-runtime.md`'s "Ollama Offline -> Recommendations unavailable -> Knowledge still searchable"), remote use not permitted (always shown for any attempted remote-eligible action in this activation, since no remote provider is registered -- `remote_not_configured`), budget exceeded, timed out, cancelled, invalid output (`schema_invalid` -- never shown as the raw model output, only as a generic "could not produce a valid explanation" state), degraded fallback and stale result. Failure never blocks the deterministic core flow -- in this activation that means Phase 3's Attention Queue continues to show scores/factors exactly as today whether or not `attention.explain_item` succeeds.

## First surface: attention-item explanation (resolved)

`attention.explain_item`'s output appears as an optional, clearly labelled "AI explanation" affordance on an attention item's existing factor list (Phase 3's `AttentionQueue.tsx`/`UX-STATES.md`'s existing "plain-language rationale" requirement) -- never replacing the deterministic factor list itself, only supplementing it. The generated text is visually distinct from deterministic content (matching `PHASE-004-ai-runtime.md`'s "Deterministic content is visually distinct" frontend-changes requirement) and always shows: which factors it cites (linked back to the same factor codes already visible), the prompt/model version used, and a discard action. No score or ranking changes as a result of requesting an explanation -- it is read-only by construction (design doc Decision 6).

## Interaction rules

Users can inspect evidence (the cited factor codes, cross-checked against the item's real factors), retry within policy (subject to the same budget as the original request, not unlimited), correct feedback (a thumbs-down/label action, recorded as `evaluation_runs`-adjacent labelled evidence, never an automatic policy change) and discard output. No anthropomorphic certainty, hidden background activity or deceptive progress -- a pending explanation shows a real, bounded progress indicator (the 20s budget, not an indefinite spinner). Core flows meet WCAG 2.2 AA, matching every existing Phase 1-3 surface.
