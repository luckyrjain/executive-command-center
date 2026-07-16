---
id: PHASE-004-EVALUATION
title: AI Evaluation Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# AI Evaluation Contract

Each AI task has a versioned dataset, rubric, required quality floor and prohibited outcomes. Metrics include schema validity, factual support, citation correctness, precision/recall where applicable, hallucination rate, latency, cost and safety failures. Human-labelled examples are separated from development examples.

Policy/model/prompt changes run comparable evaluations. Any privacy leak, unsupported consequential claim, unauthorized tool request or critical regression blocks promotion. Online feedback is labelled evidence, not an automatic policy update. Evaluation results, environment and artifact hashes are retained for reproducibility.
