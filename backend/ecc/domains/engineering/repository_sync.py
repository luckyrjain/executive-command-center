"""Shared `repositories` upsert -- extracted from `github_adapter.py`/
`gitlab_adapter.py`, which each had a byte-identical `INSERT ... ON
CONFLICT` body differing only in which provider fields fed the bind
params (Loop 2 architecture review's "`_upsert_repository` SQL
duplication" finding). Each adapter keeps its own provider-specific field
extraction (GitHub's `full_name`/`html_url` vs. GitLab's `path_with_
namespace`/`web_url`) and calls this with already-extracted values.

Opens and commits its own session -- mirrors `ecc.domains.automation.
local_adapters.LocalCreateNoteAdapter.execute`'s "no session threaded
through the adapter protocol" precedent, identical to what each adapter's
own `_upsert_repository` already did before this extraction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from ecc.database import SessionFactory

from .connectors import WORKSPACE_ORIGINAL_OWNER_SQL


def upsert_repository(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    external_id: str,
    name: str,
    source_url: str,
    default_branch: str | None,
    content_hash: str,
    provider_updated_at: Any,
    suggested_team_name: str | None,
) -> None:
    now = datetime.now(UTC)
    with SessionFactory() as session:
        session.execute(
            text(
                f"""
                INSERT INTO repositories (
                    id, workspace_id, connector_account_id, provider, external_id,
                    name, source_url, default_branch, permission_state, freshness_state,
                    content_hash, provider_updated_at, observed_at, created_at, updated_at,
                    suggested_team_name, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :provider, :external_id,
                    :name, :source_url, :default_branch, 'active', 'fresh',
                    :content_hash, :provider_updated_at, :now, :now, :now,
                    :suggested_team_name,
                    {WORKSPACE_ORIGINAL_OWNER_SQL},
                    'workspace'
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    source_url = EXCLUDED.source_url,
                    default_branch = EXCLUDED.default_branch,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    team_suggestion_dismissed_at = CASE
                        WHEN repositories.suggested_team_name
                            IS DISTINCT FROM EXCLUDED.suggested_team_name
                        THEN NULL
                        ELSE repositories.team_suggestion_dismissed_at
                    END
                """  # noqa: S608 -- WORKSPACE_ORIGINAL_OWNER_SQL is a fixed
                # module constant, never request-derived; nothing here is
                # string-interpolated user input.
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "external_id": external_id,
                "name": name,
                "source_url": source_url,
                "default_branch": default_branch,
                "content_hash": content_hash,
                "provider_updated_at": provider_updated_at,
                "now": now,
                "suggested_team_name": suggested_team_name,
            },
        )
        session.commit()
