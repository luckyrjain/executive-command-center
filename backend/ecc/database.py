from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ecc.config import get_settings

settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

# A separate, unpooled engine for session-scoped advisory locks
# (ecc.domains.ai_runtime.runtime/evaluation's `_held_idempotency_lock`).
# Those locks are held for a request's *entire* critical section, which can
# span multiple synchronous outbound model calls lasting tens of seconds to
# minutes -- drawing that connection from the app's shared, size-capped
# `engine` pool (default pool_size=5 + max_overflow=10 = 15 total,
# app-wide) would let a handful of concurrent long-running AI-runtime
# requests exhaust the pool and starve every unrelated endpoint. NullPool
# opens a fresh physical connection per checkout and closes it (rather than
# returning it to a pool) on release, so each lock-holder draws on
# Postgres's own connection ceiling instead of this app's much smaller
# self-imposed one, and deliberately does NOT get `engine`'s
# `_set_statement_timeout` listener below -- `pg_advisory_lock` is meant to
# block indefinitely until the first request releases it (that is the
# whole point of the lock), and the connect-listener's 5-second budget
# (approved for ordinary query latency, not lock-wait time) would otherwise
# cancel the wait itself under real contention.
lock_engine = create_engine(settings.database_url, poolclass=NullPool)

# Approved Phase 1 acceptance budget (see
# docs/superpowers/specs/2026-07-16-phase-1-completion-design.md:178 and
# docs/phases/phase-001/TEST-PLAN.md:57): "no query above the approved
# statement timeout". 5 seconds is the value approved for that budget.
#
# Applied as a session-level `SET statement_timeout` on every new physical
# DBAPI connection (not just asserted in a test) so it holds for every query
# issued through this engine, regardless of which endpoint or script opens
# the connection. PostgreSQL-only: SQLite (used by the default/unit-test
# database URL) has no such setting, so this is a no-op there.
STATEMENT_TIMEOUT_MS = 5_000

if settings.database_url.startswith("postgresql"):

    @event.listens_for(engine, "connect")
    def _set_statement_timeout(dbapi_connection: object, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        finally:
            cursor.close()
        # psycopg (like most DBAPI drivers) starts an implicit transaction on
        # the first statement executed on a connection, so the SET above is
        # not yet durable -- it is still inside that open transaction. The
        # pool's reset-on-return behavior issues a ROLLBACK on every checkin
        # (SQLAlchemy's default, since nothing here ever explicitly commits),
        # which undoes an uncommitted plain `SET` exactly like it would any
        # other uncommitted statement. Without this commit, the timeout reads
        # back correctly the first time this physical connection is used but
        # silently reverts to the server default (no timeout) the moment it
        # is checked back into the pool and reused -- confirmed live via a
        # second `engine.connect()` against the same pooled connection.
        dbapi_connection.commit()  # type: ignore[attr-defined]


def get_session() -> Generator[Session]:
    with SessionFactory() as session:
        yield session
