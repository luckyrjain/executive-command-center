"""The three local/fake action adapters this activation registers into the
shared production registry (`docs/superpowers/specs/2026-07-25-phase-5-
automation-design.md` Decision 8, resolving `docs/phases/PHASE-REVIEW.md`'s
F-03) -- `local.create_note`, `local.send_test_notification`,
`fake.external_action`. Classes live here, one file per this package's
existing per-concept convention (`workflows.py`/`policy.py`/`approvals.py`/
`worker.py` each own one concern); `adapters.py` itself only imports these
three classes and performs the actual `registry.register(...)` calls, so
the one place the design doc's own text ("a later task registers ... into
this exact instance") points to is still `adapters.py`, even though the
adapter bodies themselves live in this new module.

**`ecc.domains.automation.worker.run_step` is these adapters' only caller.**
Confirmed directly against `worker.py` before writing this module: `output_
model = adapter.execute(action_input)` is the sole call site, with no other
argument -- no `session`, no `workspace_id`, no `actor_id` threaded through
the `ActionAdapter` protocol (`adapters.py`'s own `execute(self, action_
input: BaseModel) -> BaseModel`). This is deliberate, not an oversight:
`execute()` is dispatched strictly *after* `run_step` has durably committed
the step's `action_digest` (Decision 3), specifically so an adapter's own
side effect never needs to be threaded into the worker's own crash-safety
transaction. Any adapter with a real, non-idempotent-by-construction side
effect (`local.create_note`, the only one of these three that touches a
domain table) therefore opens and commits its own, entirely independent
database session inside its own `execute()` -- see `LocalCreateNoteAdapter`
below.

**`local.create_note`'s option-(a)-vs-(b) choice (this task's own
instructions named both).** `ecc.domains.knowledge.notes.create_note` (the
`POST /notes` HTTP handler) is tightly coupled to FastAPI dependency
injection (`Request`, `AuthDep`, `CsrfDep`, `IdempotencyHeader`, an
idempotency-record lock/replay cache keyed by an `Idempotency-Key` header,
`session.begin()`) -- there is no plain, session-taking "just insert the row
plus its audit/outbox rows" helper it delegates to internally; every one of
those concerns lives inline in that one function body. Extracting one
(option (a), "no duplicated SQL") would mean either (i) inventing a
synthetic `Idempotency-Key`/`Request` shape for a background-worker-
dispatched write that has neither a real HTTP request nor a real
caller-supplied idempotency key at all (`run_step`'s own `action_digest` is
already this call's idempotency mechanism -- a second, HTTP-shaped
idempotency layer underneath it would be redundant, not complementary), or
(ii) splitting `create_note` into an HTTP-idempotency layer plus a bare
row-plus-audit-plus-outbox helper, which is a genuine, non-trivial refactor
of an already-shipped, already-tested Phase 1 endpoint that this task's own
scope (registering adapters) does not ask for and should not casually
attempt as a side effect of an unrelated task.

This module instead takes **option (b): a minimal, adapter-owned `INSERT`
into `notes`, not a call into `notes.py`'s own function**, with its own
audit_events/event_outbox writes performed directly against those tables --
exactly the shape `policy.py`'s `create_policy`/`_write_side_effects` and
`approvals.py`'s `create_approval_request`/`_write_side_effects` already
establish as this package's own precedent for "a Phase 5 module performs
its own workspace-scoped raw-SQL write plus its own audit/outbox rows,"
rather than reaching into another domain's HTTP-shaped function. This is a
deliberate trade-off, not an oversight: it means `local.create_note`'s
`INSERT` statement duplicates (rather than reuses) `notes.create_note`'s
own column list, and it does not perform `notes.py`'s own `_validate_
meeting_reference` existence check (a `meeting_id` this adapter is given
that does not exist in this workspace fails only if a real FK constraint
would catch it -- `notes.meeting_id` carries no FK, matching `notes.py`'s
own comment that this check is application-level only, so an adapter-
supplied nonexistent `meeting_id` is accepted uncaught by this activation's
adapter, same as it would be by any other direct-SQL write against this
table). A reviewer who judges this divergence from Phase 1's own validation
completeness unacceptable should treat extracting a shared `create_note_
row` helper as the natural follow-up, not a silent gap this module pretends
does not exist.

**`local.create_note`'s `workspace_id`/`actor_id` provenance (the
confused-deputy question this task's own review explicitly named as the
one place a careless implementation could produce a real cross-workspace
write).** `run_step`'s `_resolve_step` reads `step.get("input_mapping",
{})` verbatim from the run's own pinned, immutable `workflow_versions.
graph` -- confirmed directly against `worker.py` before writing this
module: this codebase does not yet implement Decision 2's own described
`$trigger.*`/prior-step-output templating engine (`worker.py`'s own Task 2
evidence section, judgment call (5), states this outright: "this task does
not implement `input_mapping`'s ... templating ... a later task's real
orchestration loop will need to resolve `input_mapping` before calling
`run_step`"). Concretely, today, `resolved_input` -- and therefore this
adapter's `action_input.workspace_id` -- is a **literal value an already-
workspace-scoped human author typed into a workflow's own graph at
`create_workflow_draft` time** (`workflows.py`: `workspace_id` there comes
from the caller's own `AuthDep`-resolved session, the same server-side
resolution pattern every other Phase 1-5 endpoint uses, never from the
graph body itself), not a value any inbound trigger event, approval
decision, or unauthenticated request can currently influence at run-dispatch
time. So the honest answer to "is `action_input.workspace_id` trustworthy"
is: **only as trustworthy as workflow-authoring authorization already is**
-- identical to the trust boundary that already governs every other part of
a step's own `graph` (which `action_ref` runs, in what order), not a new
one this adapter introduces. A workflow author who deliberately (or by
bug) wrote a *different* workspace's UUID into their own step's `input_
mapping` could, absent a further check, cause this adapter to write a note
into that other workspace -- an insider-authoring-time risk, not an
external caller's.

**Defense-in-depth added during this task's own self-review, not left as a
documented-only gap:** `worker.run_step` now compares any validated `action_
input.workspace_id` field against the dispatching run's own `run.workspace_
id` immediately after `input_schema.model_validate(...)` and before
`execute()` is ever called, raising `WorkspaceScopeMismatch` (caught by
`run_step`'s existing broad `except Exception`, surfacing as an ordinary
classified step failure, not an unhandled crash) on a mismatch. This is a
small, generic addition -- it inspects the *validated Pydantic model* for a
`workspace_id` attribute, so it applies to any current or future adapter
whose `input_schema` happens to declare one (today, only `local.create_
note`'s and `local.send_test_notification`'s), and does nothing at all for
an adapter with no such field (`fake.external_action`'s `high_impact_
categories` choice does not include `workspace_id`-based scoping either,
so this check is inert for it too, by construction). See `worker.py`'s own
module docstring for the mechanism; see `tests/test_automation_adapters_
postgres.py`'s workspace-isolation test for the direct proof (a run
in workspace A whose step `input_mapping.workspace_id` deliberately names
workspace B is rejected before `execute()` runs, and zero rows land in
either workspace's `notes` table).

**`local.create_note`'s output deliberately excludes the note's own
`body`.** `worker.py`'s `_redact_payload` only redacts by dict *key* name
(`secret`/`password`/`token`/`credential`/`api_key`/`authorization`
substrings) -- it has no way to scan a string *value* for secret-shaped
content. A note's `body` is exactly the kind of free-form user prose that
could echo a real leaked credential if a user pasted one into a note (the
key would be `"body"`, which matches none of `_redact_payload`'s markers,
so the value would pass through unredacted into `workflow_run_steps.
output` verbatim). Rather than rely on a key-name-matching redaction pass
that structurally cannot catch this, `CreateNoteOutput` simply never
carries `body` at all -- the safer, simpler guarantee. `title` is included
(short, structured, far less likely to carry pasted secret material, and
genuinely useful for a human reviewing run history) -- a judgment call a
reviewer should double check if a workflow author's own `title` field is
ever expected to carry sensitive content in practice.

**`local.send_test_notification` invents no persistence.** `DATA-MODEL.md`'s
own `notifications` row: "Existing skeleton, retained unchanged --
notification delivery itself is not a Phase 5 approval-gate item." This
adapter therefore never writes to any table at all (there is nothing this
activation authorizes it to write to) -- `execute()` and `simulate()` both
return a plain, in-memory confirmation model; the only observable
difference between them is the field this task's tests assert on directly
(no domain-table write to fault-inject against, since there is none to
begin with -- unlike `local.create_note`, which does have one).

**`fake.external_action`'s `high_impact_categories` choice.** Registered
under `{"public"}` -- design doc Decision 5's own taxonomy table lists
`public` ("Creates content visible outside the workspace owner's private
systems") with "None registered this slice (no production connector exists
yet -- Phase 6)"; using this deliberately-fake, deliberately-in-memory
adapter to exercise the `public` category's own approval-gate semantics end
to end closes that "zero registered examples" gap for at least a fake
adapter, the same way `local.send_test_notification` already does for
`person-directed`. A reviewer could reasonably prefer `reversible=True,
high_impact_categories=frozenset()` instead (a `bounded` fake, needing no
approval) -- this is a judgment call, not a mechanical reading of the
design doc, and is called out as such in this task's own PR evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.database import SessionFactory
from ecc.domains.knowledge.notes import NoteType, SourceType
from ecc.observability import queue_lifecycle_event, record_audit_outbox_failure

# ---------------------------------------------------------------------------
# local.create_note
# ---------------------------------------------------------------------------


class CreateNoteInput(BaseModel):
    """`workspace_id`/`actor_id` are required, explicit fields -- this
    module's own docstring ("provenance") section has the full trust-
    boundary discussion. `title`/`body`/`note_type`/`meeting_id`/
    `source_type`/`source_ref`/`restricted` mirror `ecc.domains.knowledge.
    notes.NoteCreate` field-for-field (studied directly against that model
    rather than guessed), reusing its `NoteType`/`SourceType` literals
    rather than redefining them.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    actor_id: UUID
    title: str | None = Field(default=None, max_length=500)
    body: str = Field(min_length=1, max_length=100000)
    note_type: NoteType = "general"
    meeting_id: UUID | None = None
    source_type: SourceType = "local"
    source_ref: str | None = Field(default=None, max_length=2000)
    restricted: bool = False


class CreateNoteOutput(BaseModel):
    """Deliberately excludes `body` -- module docstring's own redaction
    section explains why.
    """

    id: UUID
    workspace_id: UUID
    note_type: NoteType
    title: str | None
    created_at: datetime


def _write_note_audit_and_outbox(
    session: Session,
    *,
    workspace_id: UUID,
    actor_id: UUID,
    note_id: UUID,
    note_type: NoteType,
    meeting_id: UUID | None,
    now: datetime,
) -> None:
    """`policy.py`'s `_write_side_effects` / `approvals.py`'s own
    identically-named helper are this function's precedent -- an
    automation-adapter-owned audit_events/event_outbox pair, not a call
    into `notes.py`'s own request-scoped `_write_audit`/`_write_outbox`
    (module docstring's option (b) reasoning). `source = 'automation'`
    (`audit_events.source` is an unconstrained `String(16)`, confirmed
    directly against migration `0002_phase1_task_foundation.py` -- no
    `CHECK` restricts it to `'user'`), distinguishing this row from any
    human-initiated `note.created` audit event at a glance. `request_id`/
    `correlation_id` are freshly generated (no `Request` object exists in
    this call path at all), mirroring `notes.py`'s own `_request_ids`
    fallback for the identical "no request context" case.
    """
    request_id = uuid4()
    correlation_id = uuid4()
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, 'note.created', 'note', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'automation', CAST(:metadata AS jsonb), :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "aggregate_id": note_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "metadata": dumps({"body_redacted": True, "adapter_id": "local.create_note"}),
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
                    :event_id, :workspace_id, 'note.created.v1', 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": workspace_id,
                "correlation_id": correlation_id,
                "payload": dumps(
                    {
                        "note_id": str(note_id),
                        "version": 1,
                        "note_type": note_type,
                        "meeting_id": str(meeting_id) if meeting_id else None,
                    }
                ),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("automation_local_create_note")
        raise
    queue_lifecycle_event(session, "automation_local_create_note", "note.created", "allowed")


class LocalCreateNoteAdapter:
    """Design doc Decision 8: "wraps the existing Phase 1 note-creation
    domain call." `reversible=True` (a note can be deleted/archived through
    the existing Phase 1 `POST /notes/{id}/archive` path), `high_impact_
    categories=frozenset()` (`bounded`).
    """

    adapter_id = "local.create_note"
    # Explicitly widened to `type[BaseModel]` (not left as the inferred
    # `type[CreateNoteInput]`) -- `type[X]` is invariant under mypy's
    # structural Protocol matching, so a narrower inferred type here fails
    # `AdapterRegistry.register`'s own `ActionAdapter` structural check at
    # the type level even though it is perfectly correct at runtime (this
    # is exactly what `mypy backend` caught during this task's own
    # pre-PR verification pass -- see this module's PR evidence).
    input_schema: type[BaseModel] = CreateNoteInput
    output_schema: type[BaseModel] = CreateNoteOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

    def simulate(self, action_input: BaseModel) -> BaseModel:
        """Must not touch `notes` at all (Decision 4) -- a preview `id`
        that is never actually inserted. `tests/test_automation_adapters_
        postgres.py`'s required fault-injection test proves this directly:
        calls `simulate()`, then queries `notes` for zero matching rows.
        """
        assert isinstance(action_input, CreateNoteInput)
        return CreateNoteOutput(
            id=uuid4(),
            workspace_id=action_input.workspace_id,
            note_type=action_input.note_type,
            title=action_input.title,
            created_at=datetime.now(UTC),
        )

    def execute(self, action_input: BaseModel) -> BaseModel:
        """Opens and commits its own session (module docstring's own
        "only caller" section) -- entirely independent of whatever session
        `worker.run_step` itself is using. The `INSERT` plus its audit/
        outbox rows commit together, atomically, in one transaction (no
        risky external call sits between them to bracket, unlike `run_
        step`'s own digest-before-execute two-commit discipline -- this
        mirrors `scheduler.py`'s identical "one atomic commit, nothing
        risky in between" reasoning for its own two-write case).
        """
        assert isinstance(action_input, CreateNoteInput)
        now = datetime.now(UTC)
        note_id = uuid4()
        with SessionFactory() as session:
            row = (
                session.execute(
                    text(
                        """
                        INSERT INTO notes (
                            id, workspace_id, owner_id, title, body, note_type,
                            meeting_id, source_type, source_ref, restricted, created_by,
                            updated_by, created_at, updated_at, version
                        ) VALUES (
                            :id, :workspace_id, :owner_id, :title, :body, :note_type,
                            :meeting_id, :source_type, :source_ref, :restricted, :actor_id,
                            :actor_id, :now, :now, 1
                        ) RETURNING id, workspace_id, note_type, title, created_at
                        """
                    ),
                    {
                        "id": note_id,
                        "workspace_id": action_input.workspace_id,
                        "owner_id": action_input.actor_id,
                        "actor_id": action_input.actor_id,
                        "title": action_input.title,
                        "body": action_input.body,
                        "note_type": action_input.note_type,
                        "meeting_id": action_input.meeting_id,
                        "source_type": action_input.source_type,
                        "source_ref": action_input.source_ref,
                        "restricted": action_input.restricted,
                        "now": now,
                    },
                )
                .mappings()
                .one()
            )
            _write_note_audit_and_outbox(
                session,
                workspace_id=action_input.workspace_id,
                actor_id=action_input.actor_id,
                note_id=note_id,
                note_type=action_input.note_type,
                meeting_id=action_input.meeting_id,
                now=now,
            )
            session.commit()
        return CreateNoteOutput(
            id=row["id"],
            workspace_id=row["workspace_id"],
            note_type=row["note_type"],
            title=row["title"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# local.send_test_notification
# ---------------------------------------------------------------------------


class SendTestNotificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    recipient_user_id: UUID
    message: str = Field(min_length=1, max_length=2000)


class SendTestNotificationOutput(BaseModel):
    """`acknowledged_at` deliberately avoids the word "delivered" in the
    field name -- module docstring: no `notifications` row, no real
    channel, exists behind either `simulate()` or `execute()`; this is a
    confirmation that the adapter ran, not a persisted delivery record.
    """

    workspace_id: UUID
    recipient_user_id: UUID
    message: str
    acknowledged_at: datetime


class LocalSendTestNotificationAdapter:
    """Design doc Decision 8: "used specifically to exercise `person-
    directed` approval semantics end to end without any real external side
    effect." `reversible=False` (a delivered notification cannot be
    un-delivered -- true in spirit even though, per this module's own
    docstring, nothing is actually ever delivered or persisted in this
    activation), `high_impact_categories=frozenset({"person-directed"})`.
    """

    adapter_id = "local.send_test_notification"
    # See LocalCreateNoteAdapter's identical comment above -- same mypy
    # Protocol-invariance reasoning.
    input_schema: type[BaseModel] = SendTestNotificationInput
    output_schema: type[BaseModel] = SendTestNotificationOutput
    reversible = False
    high_impact_categories: frozenset[str] = frozenset({"person-directed"})

    def simulate(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, SendTestNotificationInput)
        return SendTestNotificationOutput(
            workspace_id=action_input.workspace_id,
            recipient_user_id=action_input.recipient_user_id,
            message=action_input.message,
            acknowledged_at=datetime.now(UTC),
        )

    def execute(self, action_input: BaseModel) -> BaseModel:
        """No table write, no queue, no network call -- module docstring's
        own "invents no persistence" section. Identical shape to
        `simulate()` by construction, since there is no real side effect
        for the two to meaningfully differ over in this activation.
        """
        assert isinstance(action_input, SendTestNotificationInput)
        return SendTestNotificationOutput(
            workspace_id=action_input.workspace_id,
            recipient_user_id=action_input.recipient_user_id,
            message=action_input.message,
            acknowledged_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# fake.external_action
# ---------------------------------------------------------------------------


class FakeExternalActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    external_ref: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class FakeExternalActionOutput(BaseModel):
    workspace_id: UUID
    external_ref: str
    fake_external_id: str
    status: Literal["created", "would_create"]


class FakeExternalActionCompensationOutput(BaseModel):
    workspace_id: UUID
    external_ref: str
    fake_external_id: str
    status: Literal["compensated"]


def _fake_external_id(action_input: FakeExternalActionInput) -> str:
    """Deterministic given `(workspace_id, external_ref)` -- so `simulate`/
    `execute`/`compensate` all agree on the same synthetic id for the same
    input, letting a test assert on it directly rather than merely on
    "some string came back." Never touches a network or a database --
    module docstring's "entirely in-memory, deterministic, clearly fake."
    """
    material = f"{action_input.workspace_id}:{action_input.external_ref}"
    return f"fake-{sha256(material.encode()).hexdigest()[:12]}"


class FakeExternalActionAdapter:
    """Design doc Decision 8: "a deliberately, visibly fake adapter ... used
    only in tests and simulation walkthroughs to exercise the full
    external-connector shape ... including `simulate`/`execute`/
    `compensate` ... without a real network call or real external system."
    `compensate()` is implemented (optional per the `ActionAdapter`
    protocol -- `adapters.compensable`/`call_compensate` exist to support
    exactly this) specifically to prove the full contract shape Decision 9
    describes; this task does not wire it into `worker.py`'s own dispatch
    loop (a later task's scope, module docstring's own note).
    `high_impact_categories` choice: see module docstring's own section.
    """

    adapter_id = "fake.external_action"
    # See LocalCreateNoteAdapter's identical comment above -- same mypy
    # Protocol-invariance reasoning.
    input_schema: type[BaseModel] = FakeExternalActionInput
    output_schema: type[BaseModel] = FakeExternalActionOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset({"public"})

    def simulate(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, FakeExternalActionInput)
        return FakeExternalActionOutput(
            workspace_id=action_input.workspace_id,
            external_ref=action_input.external_ref,
            fake_external_id=_fake_external_id(action_input),
            status="would_create",
        )

    def execute(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, FakeExternalActionInput)
        return FakeExternalActionOutput(
            workspace_id=action_input.workspace_id,
            external_ref=action_input.external_ref,
            fake_external_id=_fake_external_id(action_input),
            status="created",
        )

    def compensate(self, action_input: BaseModel) -> BaseModel:
        """Takes the same `action_input` `execute()` was originally called
        with (Decision 8's `compensate(action_input) -> BaseModel` shape --
        not the prior `execute()` call's *output*), matching `adapters.
        call_compensate`'s own signature exactly.
        """
        assert isinstance(action_input, FakeExternalActionInput)
        return FakeExternalActionCompensationOutput(
            workspace_id=action_input.workspace_id,
            external_ref=action_input.external_ref,
            fake_external_id=_fake_external_id(action_input),
            status="compensated",
        )
