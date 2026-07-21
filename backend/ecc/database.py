from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ecc.config import get_settings

settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

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
