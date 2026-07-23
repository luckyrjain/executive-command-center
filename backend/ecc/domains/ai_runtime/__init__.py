"""AI Runtime domain package (Phase 4).

Owns the provider-neutral Model Router, the model/provider registry,
versioned prompts/tools, structured-output validation, budgets/timeouts/
circuit-breakers, the bounded tool-runtime orchestration loop and the
evaluation harness (`docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-
design.md`). No other package in this repository may import the ``ollama``
Python package directly -- ``ollama_client.py`` is the sole exception
(`ADR-0004`, `ADR-0007`, `ADR-0012`).

This first activation (Task 1) ships the registry and router only:
``registry.py`` (``model_definitions`` reads), ``router.py`` (the fixed
eligibility-then-preference routing pipeline, `MODEL-ROUTING-CONTRACT.md`)
and ``ollama_client.py`` (the typed Ollama adapter). Prompt/tool versioning,
budgets, the orchestration loop and evaluation harness are later tasks in
the same plan.
"""
