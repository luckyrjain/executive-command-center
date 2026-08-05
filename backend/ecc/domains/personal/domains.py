"""Domain enablement/consent (`personal_domains`/`domain_consents`) and
generic record vault (`domain_records`/`domain_sources`), plus
`GET|POST /api/v1/personal/domains`, `POST .../{domain_key}/enable|
disable`, `GET|POST /api/v1/personal/records`, `GET|PATCH .../{id}` and
`GET|POST /api/v1/personal/consents`, `POST .../{id}/revoke`
(`docs/phases/phase-007/API-SCHEMAS.md`).

`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`
Decision 1's six `domain_key` values and three `classification` tiers are
a fixed, server-owned mapping (`_CLASSIFICATION_BY_DOMAIN` below), not a
per-request choice -- a caller names a `domain_key`, this module derives
its classification, never the other way around, so a request can never
claim a lower-stakes classification than a domain actually carries.

**Contract interpretation, disclosed rather than silently assumed.**
`API-SCHEMAS.md`'s route sketch lists `POST /personal/domains`, a
separate `POST .../{id}/enable`, and a separate `GET|POST /personal/
consents` -- three ways to reach what is, in this activation, one real
state transition (a domain becomes enabled with an active consent grant
recorded). Building three independently-meaningful endpoints for that one
transition would force intermediate states this activation's own privacy
model has no real use for (a domain "created but not enabled," or
"enabled but with no consent") -- `_enable_domain` below is the single
write path `create_domain_endpoint`, `enable_domain_endpoint` and
`create_consent_endpoint` each call, and `_disable_domain` is the single
write path both `disable_domain_endpoint` and `revoke_consent_endpoint`
call. `cross_domain_grants` (purpose-scoped grants distinct from a
domain's own top-level consent) are a real, separate future concept --
Decision 1 item 5 -- not conflated with this collapsing.

**`domain_records` field-level encryption (Task 4, `relationships`).**
`habits`/`learning`/`travel` are `standard`-classified, so no record this
module wrote before Task 4 had any encrypted field -- `_ENCRYPTED_FIELD_
NAMES_BY_RECORD_TYPE` was empty and `_redact_payload` was a structural
no-op proven against nothing. `relationships` is `sensitive`-classified
and the first domain to populate that mapping for real (`contact`/
`interaction` record types' own `notes` field, `DATA-MODEL.md`'s Task 4
section). `_encrypt_payload`/`_decrypt_payload` below wrap `ecc.domains.
personal.crypto.encrypt_field`/`decrypt_field`: encryption happens once,
immediately before `INSERT`/`UPDATE`, so the plaintext form this module
receives from a request is never itself persisted for an encrypted field;
decryption happens only for the single-record response paths (create/
get/patch) -- `list_records_endpoint` keeps using `_redact_payload`
instead, matching design doc Decision 3: "only a single-record fetch
returns the decrypted value." The idempotency cache (`idempotency_
records.response_body`) is a persisted store the decrypted value must
never reach either (matching `crypto.py`'s own "never logged, returned by
an unrelated API response, or written into an audit/outbox payload"
discipline) -- `create_record_endpoint`/`update_record_endpoint` both
build the response used for `store_idempotency` from the still-encrypted
row, and decrypt only the copy actually returned to the caller; a cache
hit (`load_cached`) decrypts the cached, still-encrypted response the
same way before returning it, so a replayed idempotent request sees the
same decrypted content as the original call without ever persisting it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.personal.crypto import decrypt_field, encrypt_field
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

DomainKey = Literal["habits", "learning", "travel", "relationships", "health", "finance", "email"]
Classification = Literal["standard", "sensitive", "high_stakes"]

# Design doc Decision 1 -- server-owned, not a request-supplied field. Only
# `habits` has a real endpoint surface in this task; the other five are
# valid `domain_key` values already (the migration's own CHECK constraint
# accepts them) so `_classification_for` never raises for a domain a later
# task will add real records/routines for, but no route in this task's own
# scope actually creates data under them yet.
_CLASSIFICATION_BY_DOMAIN: dict[str, Classification] = {
    "habits": "standard",
    "learning": "standard",
    "travel": "standard",
    "relationships": "sensitive",
    "health": "high_stakes",
    "finance": "high_stakes",
    # Phase 10 Gmail Connector design doc Decision 2: the same tier
    # `health`/`finance` use -- synced mail is at least as sensitive as
    # either.
    "email": "high_stakes",
}
# Retention nudge cadence by classification (design doc Decision 1) -- not
# yet enforced by any scheduled job in this task (no scheduler exists in
# this package), recorded here as the constant a future reconciliation job
# reads, matching Phase 6 Task 1's own disclosed "no HTTP caller yet" shape
# for `refresh_permissions`.
RETENTION_NUDGE_DAYS: dict[Classification, int | None] = {
    "standard": None,
    "sensitive": 548,  # ~18 months
    "high_stakes": 365,  # 12 months
}

# `record_type` -> the set of `payload` keys that are individually
# encrypted for a `sensitive`/`high_stakes` domain -- see module docstring.
# `relationships`' own `contact`/`interaction` record types (Task 4,
# `DATA-MODEL.md`'s Task 4 section): `notes` is the free-text field most
# likely to contain real sensitive content; `contact_name`/`relationship_
# type`/`interaction_type` are structured and low-sensitivity, so they stay
# plaintext/queryable per design doc Decision 3's "encrypt the narrative,
# not the whole row" tradeoff. `health`'s own `vital_reading`/`symptom_log`
# record types (Task 6, `DATA-MODEL.md`'s Task 6 section) follow the exact
# same split -- `symptom_description` is the field name `DOMAIN-PRIVACY-
# CONTRACT.md`'s own "Encryption fields (resolved)" section already named
# as this domain's worked example, kept consistent here rather than
# introducing a differently-named field at implementation time.
_ENCRYPTED_FIELD_NAMES_BY_RECORD_TYPE: dict[str, frozenset[str]] = {
    "contact": frozenset({"notes"}),
    "interaction": frozenset({"notes"}),
    "vital_reading": frozenset({"notes"}),
    "symptom_log": frozenset({"symptom_description", "notes"}),
    "account": frozenset({"account_notes"}),
    "transaction": frozenset({"memo"}),
}

_REDACTED_PLACEHOLDER = "***encrypted***"


def _classification_for(domain_key: str) -> Classification:
    return _CLASSIFICATION_BY_DOMAIN[domain_key]


def classification_for(domain_key: str) -> Classification:
    """Public wrapper over `_classification_for` -- for other domain-package
    modules that need a source domain's classification without duplicating
    `_CLASSIFICATION_BY_DOMAIN` (Task 5 part 2's `insight_tools.py:get_
    insight_sources_tool`, which must know whether any source domain a
    cross-domain insight drew from is `high_stakes` before the AI runtime
    can require a `professional_referral_note`; `evaluation.py`'s own
    synthetic-source seeding for this same task type).
    """
    return _classification_for(domain_key)


def _redact_payload(record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Used only by list/summary responses -- a single-record fetch
    (`get_record_endpoint`) returns `payload` unredacted. Matches design
    doc Decision 3: "only a single-record fetch returns the decrypted
    value."
    """
    encrypted_fields = _ENCRYPTED_FIELD_NAMES_BY_RECORD_TYPE.get(record_type, frozenset())
    if not encrypted_fields:
        return payload
    return {k: (_REDACTED_PLACEHOLDER if k in encrypted_fields else v) for k, v in payload.items()}


def _encrypt_payload(record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Called immediately before `INSERT`/`UPDATE` on `create_record_
    endpoint`/`update_record_endpoint` -- the plaintext form of an
    encrypted field is never itself persisted. A missing or non-`str`
    value for a listed field is left as-is (an optional encrypted field
    the caller simply didn't supply, e.g. `notes` on a `contact` record
    with no notes yet, has nothing to encrypt).
    """
    encrypted_fields = _ENCRYPTED_FIELD_NAMES_BY_RECORD_TYPE.get(record_type, frozenset())
    if not encrypted_fields:
        return payload
    result = dict(payload)
    for field in encrypted_fields:
        value = result.get(field)
        if isinstance(value, str) and value:
            result[field] = encrypt_field(value)
    return result


def _decrypt_payload(record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Inverse of `_encrypt_payload` -- called only for the single-record
    response paths (create/get/patch), never list/summary or anything
    persisted (the module docstring's idempotency-cache note explains why).
    Also the single choke point every OTHER caller of a decrypted payload
    goes through (`decrypt_record_payload`'s own docstring): `export_
    domain_endpoint` and `insight_tools.py:get_insight_sources_tool`.

    `decrypt_field` raises `cryptography.fernet.InvalidToken` if a field's
    ciphertext doesn't match the currently configured key (e.g. `ECC_
    PERSONAL_DATA_ENCRYPTION_KEY` rotated without re-encrypting stored
    records) -- caught per field rather than left to propagate, mirroring
    `connector_accounts.py`'s own established fix for the identical bug
    class ("previously that propagated unhandled ... 500ing the request").
    Left unhandled here, ONE such field would 500 `GET /records/{id}`, fail
    `POST /domains/{domain_key}/export` for every record in that domain (not
    just the bad one -- a real data-export right becoming unusable until the
    row is fixed), and crash `personal.get_insight_sources` out of `execute_
    run` with an unhandled 500 instead of `ai_insights.py`'s own documented
    fail-open `available=False`. The placeholder is deliberately distinct
    from `_redact_payload`'s `***encrypted***` (a normal, expected state for
    a list view) -- this is an abnormal one, and says so.
    """
    encrypted_fields = _ENCRYPTED_FIELD_NAMES_BY_RECORD_TYPE.get(record_type, frozenset())
    if not encrypted_fields:
        return payload
    result = dict(payload)
    for field in encrypted_fields:
        value = result.get(field)
        if isinstance(value, str) and value:
            try:
                result[field] = decrypt_field(value)
            except InvalidToken:
                result[field] = "[unable to decrypt -- contact support]"
    return result


def decrypt_record_payload(record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Public wrapper over `_decrypt_payload` -- for other domain-package
    modules that need a cross-domain record's payload decrypted the same
    way a single-record fetch/export already does (Task 5 part 2's
    `insight_tools.py:get_insight_sources_tool`: a model reasoning about a
    granted domain's records must see the real content, never the
    `_redact_payload` placeholder `list_records_endpoint` returns).
    """
    return _decrypt_payload(record_type, payload)


def encrypt_record_payload(record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Public wrapper over `_encrypt_payload` -- for `evaluation.py`'s own
    synthetic-source seeding for `personal.generate_insight` (Task 5 part
    2), which must persist `domain_records.payload` encrypted at rest
    exactly like a real write would, so the evaluation harness genuinely
    exercises the tool's own decrypt-before-prompting path rather than
    handing the model already-plaintext synthetic data no real record
    would ever have.
    """
    return _encrypt_payload(record_type, payload)


# ---------------------------------------------------------------------------
# Shared rows/dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonalDomain:
    id: UUID
    domain_key: str
    classification: Classification
    enabled: bool
    enabled_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


def _row_to_domain(row: dict[str, Any]) -> PersonalDomain:
    return PersonalDomain(
        id=row["id"],
        domain_key=row["domain_key"],
        classification=row["classification"],
        enabled=row["enabled"],
        enabled_at=row["enabled_at"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_DOMAIN_FIELDS = (
    "id, domain_key, classification, enabled, enabled_at, version, created_at, updated_at"
)


def get_domain(
    session: Session, auth: AuthContext, domain_key: str, *, for_update: bool = False
) -> PersonalDomain | None:
    """`for_update=True` locks the row for the caller's own transaction --
    used by `_enable_domain`/`_disable_domain` (both mutate this row) so a
    second concurrent enable/disable for the same domain blocks until the
    first transaction commits, mirroring `connector_accounts.
    get_connector_account`'s identical `for_update` precedent.
    """
    clause = "FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"SELECT {_DOMAIN_FIELDS} FROM personal_domains "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                f"AND domain_key = :domain_key {clause}"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "domain_key": domain_key},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_domain(dict(row)) if row is not None else None


def list_domains(session: Session, auth: AuthContext) -> list[PersonalDomain]:
    rows = (
        session.execute(
            text(
                f"SELECT {_DOMAIN_FIELDS} FROM personal_domains "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "ORDER BY created_at ASC"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id},
        )
        .mappings()
        .all()
    )
    return [_row_to_domain(dict(row)) for row in rows]


def require_enabled_domain(session: Session, auth: AuthContext, domain_key: str) -> PersonalDomain:
    """Every write under a domain (`domain_records`, and `habits.py`'s
    `goals`/`routines`/`check_ins`) must go through this -- a domain that
    was never enabled, or was explicitly disabled, must reject every write
    attempt under it, matching `DOMAIN-PRIVACY-CONTRACT.md`'s "Enabling
    one does not grant another access" applied to the disabled case too:
    disabled means no new collection, not merely a UI hint.
    """
    domain = get_domain(session, auth, domain_key)
    if domain is None or not domain.enabled:
        raise HTTPException(status_code=403, detail="DOMAIN_NOT_ENABLED")
    return domain


# ---------------------------------------------------------------------------
# Idempotency/audit helpers, shared across every mutating endpoint in this
# package (`domains.py`/`habits.py`/`export_deletion.py` all import these) --
# mirrors `notes.py`/`connector_accounts.py`'s identical per-module helpers,
# consolidated once here specifically because this package's own generic
# vault framework has many more independent mutating resources (domains,
# records, goals, routines, check-ins) than either of those single-resource
# precedents, and hand-duplicating this shape five more times would be
# exactly the kind of repetition worth naming once instead.
# ---------------------------------------------------------------------------


def request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def load_cached(
    session: Session, auth: AuthContext, key: str, req_hash: str, *, domain: str
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body FROM idempotency_records
                WHERE workspace_id = :workspace_id AND actor_id = :actor_id
                  AND key = :key AND expires_at > :now
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": key,
                "now": datetime.now(UTC),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["request_hash"] != req_hash:
        record_idempotency_conflict(domain)
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    result: dict[str, Any] = row["response_body"]
    return result


def store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    req_hash: str,
    response_body: dict[str, Any],
    now: datetime,
    response_status: int = 200,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, :response_status,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": req_hash,
            "response_status": response_status,
            "response_body": dumps(response_body),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    *,
    domain: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    version: int,
    now: datetime,
) -> None:
    """Personal-domain content never appears in `changed_fields`/audit
    metadata beyond the aggregate id/type/version already recorded for
    every other domain's mutations -- `RFC-005`'s "sensitive source content
    MUST NOT be logged" discipline applies to the audit trail exactly as it
    does to application logs.
    """
    request_id, correlation_id = request_ids(request)
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "occurred_at": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, payload, occurred_at, attempt_count
                ) VALUES (
                    :event_id, :workspace_id, :event_type_v1, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type_v1": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"aggregate_id": str(aggregate_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure(domain)
        raise
    queue_lifecycle_event(session, domain, event_type, "allowed")


# ---------------------------------------------------------------------------
# GET|POST /api/v1/personal/domains, enable/disable, consents
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/personal", tags=["personal"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class DomainEnableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain_key: DomainKey


class DomainResponse(BaseModel):
    id: UUID
    domain_key: str
    classification: Classification
    enabled: bool
    enabled_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class DomainListResponse(BaseModel):
    domains: list[DomainResponse]


class ConsentResponse(BaseModel):
    id: UUID
    domain_key: str
    granted_at: datetime
    revoked_at: datetime | None


class ConsentListResponse(BaseModel):
    consents: list[ConsentResponse]


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _to_domain_response(domain: PersonalDomain) -> DomainResponse:
    return DomainResponse(
        id=domain.id,
        domain_key=domain.domain_key,
        classification=domain.classification,
        enabled=domain.enabled,
        enabled_at=domain.enabled_at,
        version=domain.version,
        created_at=domain.created_at,
        updated_at=domain.updated_at,
    )


@router.get("/domains", response_model=DomainListResponse)
def list_domains_endpoint(auth: AuthDep, session: SessionDep) -> DomainListResponse:
    return DomainListResponse(domains=[_to_domain_response(d) for d in list_domains(session, auth)])


def _enable_domain(
    payload: DomainEnableRequest,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
) -> DomainResponse:
    """Idempotent enable-with-consent. If `personal_domains` has no row for
    this `(workspace, owner, domain_key)` yet, creates one `enabled=true`;
    if a row already exists (previously enabled or disabled), re-enables it
    and records a fresh `domain_consents` grant -- re-enabling a disabled
    domain is a new, deliberate consent event, not a resumption of the old
    one (`DOMAIN-PRIVACY-CONTRACT.md`: consent is "time-bound and
    revocable," so a lapsed consent does not silently resurrect itself).

    Two concurrent first-time enables for the same domain (different
    `Idempotency-Key`s, so `lock_idempotency`'s advisory lock does not
    itself serialize them) would otherwise both read `existing is None`
    and both attempt the `INSERT`, the loser hitting `uq_personal_domains_
    owner_domain` -- an unhandled `IntegrityError`/500 rather than the
    correct outcome (the loser should just re-enable the winner's row, the
    same "enable" a solo caller would get). The `INSERT` runs inside a
    `begin_nested()` SAVEPOINT so a losing conflict rolls back only that
    savepoint, not this call's whole transaction, then falls through to the
    identical `UPDATE`/`FOR UPDATE`-locked-read path `existing is not None`
    already uses -- mirrors `create_connector_endpoint`'s own identical
    `IntegrityError`-after-`begin_nested()` precedent.
    """
    req_hash = request_hash(payload, "enable_domain")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="personal_domains")
        if cached is not None:
            return DomainResponse.model_validate(cached)

        classification = _classification_for(payload.domain_key)
        existing = get_domain(session, auth, payload.domain_key, for_update=True)
        domain_id: UUID | None = None
        if existing is None:
            domain_id = uuid4()
            try:
                with session.begin_nested():
                    session.execute(
                        text(
                            """
                            INSERT INTO personal_domains (
                                id, workspace_id, owner_id, domain_key, classification,
                                enabled, enabled_at, created_by, updated_by,
                                created_at, updated_at, version
                            ) VALUES (
                                :id, :workspace_id, :owner_id, :domain_key, :classification,
                                true, :now, :actor_id, :actor_id, :now, :now, 1
                            )
                            """
                        ),
                        {
                            "id": domain_id,
                            "workspace_id": auth.workspace_id,
                            "owner_id": auth.user_id,
                            "domain_key": payload.domain_key,
                            "classification": classification,
                            "now": now,
                            "actor_id": auth.user_id,
                        },
                    )
            except IntegrityError:
                # A concurrent first-enable won the race -- re-read (now
                # locked) and fall through to the update branch below.
                existing = get_domain(session, auth, payload.domain_key, for_update=True)
                assert existing is not None
                domain_id = None
            else:
                version = 1

        if existing is not None:
            domain_id = existing.id
            session.execute(
                text(
                    """
                    UPDATE personal_domains SET enabled = true, enabled_at = :now,
                        updated_at = :now, updated_by = :actor_id, version = version + 1
                    WHERE id = :id
                    """
                ),
                {"now": now, "actor_id": auth.user_id, "id": domain_id},
            )
            version = existing.version + 1
        assert domain_id is not None

        session.execute(
            text(
                """
                INSERT INTO domain_consents (
                    id, workspace_id, owner_id, domain_key, granted_at, created_at
                ) VALUES (:id, :workspace_id, :owner_id, :domain_key, :now, :now)
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": payload.domain_key,
                "now": now,
            },
        )

        domain = get_domain(session, auth, payload.domain_key)
        assert domain is not None
        response = _to_domain_response(domain)
        write_side_effects(
            session,
            auth,
            request,
            domain="personal_domains",
            event_type="personal_domain.enabled",
            aggregate_type="personal_domain",
            aggregate_id=domain.id,
            version=version,
            now=now,
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/domains", response_model=DomainResponse, status_code=status.HTTP_201_CREATED)
def create_domain_endpoint(
    payload: DomainEnableRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DomainResponse:
    return _enable_domain(payload, request, auth, session, idempotency_key)


@router.post("/domains/{domain_key}/enable", response_model=DomainResponse)
def enable_domain_endpoint(
    domain_key: DomainKey,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DomainResponse:
    return _enable_domain(
        DomainEnableRequest(domain_key=domain_key), request, auth, session, idempotency_key
    )


def _disable_domain(
    domain_key: str,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
) -> DomainResponse:
    req_hash = request_hash(_EmptyBody(), f"disable_domain:{domain_key}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="personal_domains")
        if cached is not None:
            return DomainResponse.model_validate(cached)

        domain = get_domain(session, auth, domain_key, for_update=True)
        if domain is None:
            raise HTTPException(status_code=404, detail="DOMAIN_NOT_FOUND")

        version = domain.version
        if domain.enabled:
            session.execute(
                text(
                    """
                    UPDATE personal_domains SET enabled = false,
                        updated_at = :now, updated_by = :actor_id, version = version + 1
                    WHERE id = :id
                    """
                ),
                {"now": now, "actor_id": auth.user_id, "id": domain.id},
            )
            session.execute(
                text(
                    """
                    UPDATE domain_consents SET revoked_at = :now
                    WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                      AND domain_key = :domain_key AND revoked_at IS NULL
                    """
                ),
                {
                    "now": now,
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "domain_key": domain_key,
                },
            )
            version = domain.version + 1

        refreshed = get_domain(session, auth, domain_key)
        assert refreshed is not None
        response = _to_domain_response(refreshed)
        write_side_effects(
            session,
            auth,
            request,
            domain="personal_domains",
            event_type="personal_domain.disabled",
            aggregate_type="personal_domain",
            aggregate_id=domain.id,
            version=version,
            now=now,
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/domains/{domain_key}/disable", response_model=DomainResponse)
def disable_domain_endpoint(
    domain_key: DomainKey,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DomainResponse:
    return _disable_domain(domain_key, request, auth, session, idempotency_key)


@router.get("/consents", response_model=ConsentListResponse)
def list_consents_endpoint(auth: AuthDep, session: SessionDep) -> ConsentListResponse:
    rows = (
        session.execute(
            text(
                "SELECT id, domain_key, granted_at, revoked_at FROM domain_consents "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "ORDER BY granted_at DESC"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id},
        )
        .mappings()
        .all()
    )
    return ConsentListResponse(consents=[ConsentResponse(**dict(r)) for r in rows])


@router.post("/consents", response_model=DomainResponse, status_code=status.HTTP_201_CREATED)
def create_consent_endpoint(
    payload: DomainEnableRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DomainResponse:
    """Same write path as `POST /personal/domains` -- see module docstring's
    "contract interpretation" section for why this activation treats
    granting a domain's consent and enabling it as one transition, not two.
    """
    return _enable_domain(payload, request, auth, session, idempotency_key)


@router.post("/consents/{consent_id}/revoke", response_model=DomainResponse)
def revoke_consent_endpoint(
    consent_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DomainResponse:
    # Own explicit transaction, committed (closed) before `_disable_domain`
    # opens its own -- `Session`'s autobegin means a bare `session.execute`
    # here would otherwise leave an open transaction behind, and `_disable_
    # domain`'s own `with session.begin()` would then raise
    # `InvalidRequestError` ("a transaction is already begun"), exactly the
    # bug `list_insights_endpoint`'s own docstring documents for the
    # identical shape.
    with session.begin():
        row = (
            session.execute(
                text(
                    "SELECT domain_key FROM domain_consents "
                    "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :id"
                ),
                {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": consent_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="CONSENT_NOT_FOUND")
    return _disable_domain(row["domain_key"], request, auth, session, idempotency_key)


# ---------------------------------------------------------------------------
# GET|POST /api/v1/personal/records, GET|PATCH .../{id}
# ---------------------------------------------------------------------------


class RecordCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain_key: DomainKey
    record_type: str = Field(min_length=1, max_length=50)
    payload: dict[str, Any] = Field(default_factory=dict)
    effective_at: datetime | None = None
    domain_source_id: UUID | None = None
    # Design doc's "Retention (resolved)": every `high_stakes` `domain_
    # records` row requires `retention_acknowledged_at` set at creation
    # time -- consent affirmed per record, not only once at domain
    # enablement. Ignored (never required, but still honestly recorded if
    # supplied) for `standard`/`sensitive` domains, where this
    # per-record acknowledgement has no defined meaning.
    retention_acknowledged: bool = False


class RecordPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    payload: dict[str, Any]


class RecordResponse(BaseModel):
    id: UUID
    domain_key: str
    record_type: str
    payload: dict[str, Any]
    domain_source_id: UUID | None
    effective_at: datetime
    retention_acknowledged_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class RecordListResponse(BaseModel):
    records: list[RecordResponse]


_RECORD_FIELDS = (
    "id, domain_key, record_type, payload, domain_source_id, effective_at, "
    "retention_acknowledged_at, version, created_at, updated_at"
)


def _get_record_row(
    session: Session, auth: AuthContext, record_id: UUID, *, for_update: bool = False
) -> dict[str, Any] | None:
    """`for_update=True` (used only by `update_record_endpoint`) locks the
    row for the caller's own transaction, mirroring `connector_accounts.
    get_connector_account`'s identical `for_update` precedent -- without
    it, two concurrent `PATCH` calls quoting the same `expected_version`
    could both pass the version check before either commits its own
    `UPDATE`, silently losing one caller's write instead of the second one
    correctly getting `VERSION_CONFLICT` (`repositories.team_assignment_
    version`'s own `SELECT ... FOR UPDATE` in `connector_accounts.py` is
    the established fix for this exact race in this codebase).
    """
    clause = "FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"SELECT {_RECORD_FIELDS} FROM domain_records "
                f"WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :id {clause}"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": record_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


@router.post("/records", response_model=RecordResponse, status_code=status.HTTP_201_CREATED)
def create_record_endpoint(
    payload: RecordCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RecordResponse:
    req_hash = request_hash(payload, "create_record")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="personal_record")
        if cached is not None:
            # `cached["payload"]` is still in its encrypted-at-rest form
            # (see module docstring) -- decrypt only the copy returned to
            # this caller, never the persisted idempotency record itself.
            decrypted = dict(cached)
            decrypted["payload"] = _decrypt_payload(cached["record_type"], cached["payload"])
            return RecordResponse.model_validate(decrypted)

        domain = require_enabled_domain(session, auth, payload.domain_key)
        if domain.classification == "high_stakes" and not payload.retention_acknowledged:
            raise HTTPException(status_code=422, detail="RETENTION_ACKNOWLEDGEMENT_REQUIRED")
        if payload.domain_source_id is not None:
            # `domain_records.domain_source_id`'s own FK (migration `0054`)
            # is scoped to `(workspace_id, id)` only, not `owner_id` -- no
            # endpoint in this activation creates `domain_sources` rows yet
            # (manual entry only), so this is unreachable today, but a
            # future import endpoint would otherwise let a record be linked
            # to another owner's `domain_sources` row within the same
            # workspace with no application-level check catching it either.
            source_owner = session.execute(
                text(
                    "SELECT owner_id FROM domain_sources "
                    "WHERE workspace_id = :workspace_id AND id = :domain_source_id"
                ),
                {"workspace_id": auth.workspace_id, "domain_source_id": payload.domain_source_id},
            ).scalar_one_or_none()
            if source_owner is None or source_owner != auth.user_id:
                raise HTTPException(status_code=404, detail="DOMAIN_SOURCE_NOT_FOUND")

        record_id = uuid4()
        effective_at = payload.effective_at or now
        encrypted_payload = _encrypt_payload(payload.record_type, payload.payload)
        session.execute(
            text(
                """
                INSERT INTO domain_records (
                    id, workspace_id, owner_id, domain_key, record_type, payload,
                    domain_source_id, effective_at, retention_acknowledged_at,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, :record_type,
                    CAST(:payload AS jsonb), :domain_source_id, :effective_at,
                    :retention_acknowledged_at, :actor_id, :actor_id, :now, :now, 1
                )
                """
            ),
            {
                "id": record_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": payload.domain_key,
                "record_type": payload.record_type,
                "payload": dumps(encrypted_payload),
                "domain_source_id": payload.domain_source_id,
                "effective_at": effective_at,
                "retention_acknowledged_at": now if payload.retention_acknowledged else None,
                "now": now,
                "actor_id": auth.user_id,
            },
        )
        row = _get_record_row(session, auth, record_id)
        assert row is not None
        write_side_effects(
            session,
            auth,
            request,
            domain="personal_record",
            event_type="domain_record.created",
            aggregate_type="domain_record",
            aggregate_id=record_id,
            version=1,
            now=now,
        )
        # `row["payload"]` is still encrypted -- this is the form persisted
        # to the idempotency cache (module docstring's own note on why the
        # decrypted value must never reach that table). Only the response
        # actually returned to this caller is decrypted, built afterward.
        stored_response = RecordResponse(**row)
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            stored_response.model_dump(mode="json"),
            now,
            response_status=status.HTTP_201_CREATED,
        )
        row["payload"] = _decrypt_payload(row["record_type"], row["payload"])
        response = RecordResponse(**row)
        return response


@router.get("/records", response_model=RecordListResponse)
def list_records_endpoint(
    auth: AuthDep,
    session: SessionDep,
    domain_key: Annotated[DomainKey | None, Query()] = None,
    # `travel` (Task 3) is the first domain whose records are meaningfully
    # time-ranged (a trip's own `effective_at` is its start date) --
    # generic rather than travel-specific, since any domain's records can
    # already carry an `effective_at`, matching this module's existing
    # "a caller names a domain_key, never a domain-specific code path"
    # convention (this module's own docstring).
    effective_from: Annotated[datetime | None, Query()] = None,
    effective_until: Annotated[datetime | None, Query()] = None,
) -> RecordListResponse:
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id}
    where = "workspace_id = :workspace_id AND owner_id = :owner_id"
    if domain_key is not None:
        where += " AND domain_key = :domain_key"
        params["domain_key"] = domain_key
    if effective_from is not None:
        where += " AND effective_at >= :effective_from"
        params["effective_from"] = effective_from
    if effective_until is not None:
        where += " AND effective_at <= :effective_until"
        params["effective_until"] = effective_until
    rows = (
        session.execute(
            text(
                f"SELECT {_RECORD_FIELDS} FROM domain_records WHERE {where} "
                "ORDER BY effective_at DESC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    records = []
    for row in rows:
        data = dict(row)
        data["payload"] = _redact_payload(data["record_type"], data["payload"])
        records.append(RecordResponse(**data))
    return RecordListResponse(records=records)


@router.get("/records/{record_id}", response_model=RecordResponse)
def get_record_endpoint(record_id: UUID, auth: AuthDep, session: SessionDep) -> RecordResponse:
    row = _get_record_row(session, auth, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="RECORD_NOT_FOUND")
    row["payload"] = _decrypt_payload(row["record_type"], row["payload"])
    return RecordResponse(**row)


@router.patch("/records/{record_id}", response_model=RecordResponse)
def update_record_endpoint(
    record_id: UUID,
    payload: RecordPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RecordResponse:
    req_hash = request_hash(payload, f"update_record:{record_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="personal_record")
        if cached is not None:
            # See create_record_endpoint's identical note -- `cached["payload"]`
            # is still encrypted-at-rest; decrypt only the returned copy.
            decrypted = dict(cached)
            decrypted["payload"] = _decrypt_payload(cached["record_type"], cached["payload"])
            return RecordResponse.model_validate(decrypted)

        existing = _get_record_row(session, auth, record_id, for_update=True)
        if existing is None:
            raise HTTPException(status_code=404, detail="RECORD_NOT_FOUND")
        if existing["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")

        encrypted_payload = _encrypt_payload(existing["record_type"], payload.payload)
        session.execute(
            text(
                """
                UPDATE domain_records SET payload = CAST(:payload AS jsonb),
                    updated_at = :now, updated_by = :actor_id, version = version + 1
                WHERE id = :id
                """
            ),
            {
                "payload": dumps(encrypted_payload),
                "now": now,
                "actor_id": auth.user_id,
                "id": record_id,
            },
        )
        row = _get_record_row(session, auth, record_id)
        assert row is not None
        write_side_effects(
            session,
            auth,
            request,
            domain="personal_record",
            event_type="domain_record.updated",
            aggregate_type="domain_record",
            aggregate_id=record_id,
            version=row["version"],
            now=now,
        )
        # Same "encrypted form persisted, decrypted form returned" split as
        # create_record_endpoint.
        stored_response = RecordResponse(**row)
        store_idempotency(
            session, auth, idempotency_key, req_hash, stored_response.model_dump(mode="json"), now
        )
        row["payload"] = _decrypt_payload(row["record_type"], row["payload"])
        response = RecordResponse(**row)
        return response
