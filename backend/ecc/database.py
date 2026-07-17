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


def get_session() -> Generator[Session]:
    with SessionFactory() as session:
        yield session
