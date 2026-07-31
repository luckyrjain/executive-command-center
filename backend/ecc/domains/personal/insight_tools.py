"""`personal.get_insight_sources` tool handler (Phase 7 Task 5 part 2): the
deterministic required-input fetch for the `personal.generate_insight` AI
task type (`docs/phases/phase-007/INSIGHT-CONTRACT.md`: "Cross-domain
insight requires an active grant covering every source").

Reads `domain_records` for every domain named in the caller's
`source_domain_keys`, but only after confirming, for **each** one:

1. The domain is currently enabled (`personal_domains.enabled`) -- not
   merely "was enabled when some grant was created". A domain a user later
   disabled must not have its data read into a new insight, matching
   `domains.py:require_enabled_domain`'s "disabled means no new
   collection, not merely a UI hint" discipline applied here too, even
   though disabling a domain does not itself revoke any `cross_domain_
   grants` row naming it (Task 5 part 1 scope: grant revocation is its own
   explicit action, not cascaded from domain disablement).
2. An active grant exists for it -- non-revoked, non-expired,
   `purpose='insight_generation'` (`cross_domain_grants`, Task 5 part 1).

Missing either condition for even *one* requested domain fails the whole
call (`ToolNotFound`, not a partial read of only the domains that did
qualify) -- `INSIGHT-CONTRACT.md`'s "requires an active grant covering
*every* source" is a conjunction, not a best-effort filter.

`granted_categories` on each domain's active grant(s) further narrows which
`record_type`s are actually readable -- a grant naming only
`["habit_check_in"]` never exposes a different `record_type` under that
same domain even if some other, still-active grant for it would allow
that. (A user may hold more than one active grant for the same domain with
different category lists; this handler reads the union of all of them.)

Decrypts every returned record's payload (`domains.decrypt_record_payload`)
-- the same single-record-fetch/export decryption path `GET /personal/
records/{id}` and `POST .../export` already use, never the list-view
`_redact_payload` placeholder, since this tool's output is fed to a prompt
that must reason about the actual content, not a redaction marker.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult

from .domains import classification_for, decrypt_record_payload

# Decision 6's "every tool result ... is size-bounded" -- caps how much of
# one domain's records this tool ever hands to a prompt, the same
# discipline `meeting_prep_tools.py`'s own `_MAX_EVIDENCE` applies. Ordered
# by `effective_at DESC` (most recent first, matching `list_records_
# endpoint`'s own default ordering) so a capped read still keeps the most
# recent, most relevant records rather than an arbitrary/oldest slice.
_MAX_RECORDS_PER_DOMAIN = 50


def _is_domain_enabled(session: Session, auth: AuthContext, domain_key: str) -> bool:
    row = session.execute(
        text(
            "SELECT enabled FROM personal_domains "
            "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
            "AND domain_key = :domain_key"
        ),
        {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "domain_key": domain_key},
    ).first()
    return row is not None and bool(row[0])


def _active_grant_categories(
    session: Session, auth: AuthContext, domain_key: str
) -> list[str] | None:
    """The union of `granted_categories` across every currently-active
    grant for this domain -- `None` if no active grant exists at all,
    distinct from an active grant with an empty category list (schema-
    impossible: `grants.py:GrantCreateRequest.granted_categories` requires
    `min_length=1`, so an empty list can never mean "an active grant
    exists but grants nothing").
    """
    rows = (
        session.execute(
            text(
                """
                SELECT granted_categories FROM cross_domain_grants
                WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                  AND source_domain_key = :domain_key AND purpose = 'insight_generation'
                  AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": domain_key,
            },
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    categories: set[str] = set()
    for row in rows:
        categories.update(row["granted_categories"])
    return sorted(categories)


def get_insight_sources_tool(
    session: Session, auth: AuthContext, source_domain_keys: list[str]
) -> ToolResult | ToolNotFound:
    sources: list[dict[str, Any]] = []
    for domain_key in source_domain_keys:
        if not _is_domain_enabled(session, auth, domain_key):
            return ToolNotFound(tool="personal.get_insight_sources")

        categories = _active_grant_categories(session, auth, domain_key)
        if categories is None:
            return ToolNotFound(tool="personal.get_insight_sources")

        rows = (
            session.execute(
                text(
                    """
                    SELECT id, record_type, payload, effective_at FROM domain_records
                    WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                      AND domain_key = :domain_key AND record_type = ANY(:categories)
                    ORDER BY effective_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "domain_key": domain_key,
                    "categories": categories,
                    "limit": _MAX_RECORDS_PER_DOMAIN,
                },
            )
            .mappings()
            .all()
        )
        sources.append(
            {
                "domain_key": domain_key,
                "classification": classification_for(domain_key),
                "records": [
                    {
                        "id": str(row["id"]),
                        "record_type": row["record_type"],
                        "payload": decrypt_record_payload(row["record_type"], row["payload"]),
                        "effective_at": row["effective_at"].isoformat(),
                    }
                    for row in rows
                ],
            }
        )
    return ToolResult(output={"sources": sources})
