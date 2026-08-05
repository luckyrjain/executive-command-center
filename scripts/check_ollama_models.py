"""Verify the local Ollama server has every model this deployment's
``model_definitions`` catalog (Phase 4 AI Runtime) requires.

Standalone by design, mirroring ``bootstrap_dev.py``'s own shape: only
``psycopg`` and the standard library, no ``ecc`` import (scripts/ is not on
the package's import path -- see ``bootstrap_dev.py``'s own comment
precedent). ``ecc.domains.ai_runtime.ollama_client``'s
``DEFAULT_OLLAMA_HOST`` is the sole place a host is ever configured for a
real request -- there is no env override in this codebase today -- so
``_OLLAMA_HOST`` below is a literal copy of that same constant, not an
independent value.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import psycopg

_OLLAMA_HOST = "http://127.0.0.1:11434"


def _database_url() -> str:
    value = os.getenv(
        "ECC_DATABASE_URL",
        "postgresql+psycopg://ecc:ecc@localhost:5432/ecc",
    )
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value


def _required_models() -> list[str]:
    with psycopg.connect(_database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_id
                FROM model_definitions
                WHERE provider = 'ollama' AND status = 'active'
                ORDER BY model_id ASC
                """
            )
            return [row[0] for row in cursor.fetchall()]


def _installed_models() -> list[str] | None:
    """Returns None if the Ollama server is unreachable (distinct from an
    empty install)."""
    request = urllib.request.Request(f"{_OLLAMA_HOST}/api/tags")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return [model["name"] for model in payload.get("models", [])]


def main() -> int:
    required = _required_models()
    if not required:
        print("No active Ollama models registered in model_definitions -- nothing to check.")
        return 0

    installed = _installed_models()
    if installed is None:
        print(
            f"Ollama server not reachable at {_OLLAMA_HOST}.\n"
            "AI enrichment features (meeting prep summaries, attention explanations, "
            "personal insights) will stay unavailable until it is running; the "
            "deterministic core works regardless.\n"
            "Start it with `ollama serve`, then rerun this check."
        )
        return 1

    missing = [model_id for model_id in required if model_id not in installed]
    if not missing:
        print(f"All {len(required)} required Ollama model(s) present: {', '.join(required)}")
        return 0

    print(f"Missing {len(missing)} of {len(required)} required Ollama model(s):")
    for model_id in missing:
        print(f"  - {model_id}")
    print("\nPull the missing model(s):\n")
    for model_id in missing:
        print(f"  ollama pull {model_id}")
    print(
        "\nAI enrichment features stay unavailable (fail closed to the deterministic "
        "core) until these are pulled -- not a hard blocker for local dev."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
