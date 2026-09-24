"""Sensitive-logging hygiene for the shared SQLAlchemy engines (Spec A S1.6,
threat T7): `ecc.database.engine` and `lock_engine` are created with
`hide_parameters=True`, so a database error raised through the app's engines
or sessions never carries bound parameter values (emails, credential bytes,
OAuth-derived identifiers) in its exception text or in SQLAlchemy's own
statement logging.

The violation used here is deliberately one where PostgreSQL's own `DETAIL`
line does *not* echo the sentinel: the unique key is on column `k`, and the
sentinel lives only in the non-key column `secret`, so the only place it
could appear is SQLAlchemy's `[parameters: ...]` suffix -- exactly what
`hide_parameters` controls. (The driver's `DETAIL: Key (...)=(...)` text is
outside that option's reach; see `ecc.platform.connector_security.
integrity_error_log_fields` for how callers avoid logging it.)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ecc.config import get_settings
from ecc.database import SessionFactory, engine, lock_engine

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SENTINEL = "log-hygiene-sentinel-owner@example.test"
_HIDDEN_MARKER = "SQL parameters hidden due to hide_parameters=True"


def _create_temp_table(connection: Connection) -> None:
    connection.execute(
        text("CREATE TEMP TABLE log_hygiene_probe (k integer UNIQUE, secret text NOT NULL)")
    )


def _insert(connection: Connection) -> None:
    connection.execute(
        text("INSERT INTO log_hygiene_probe (k, secret) VALUES (:k, :secret)"),
        {"k": 1, "secret": _SENTINEL},
    )


@pytest.fixture
def sqlalchemy_engine_logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Turn SQLAlchemy's statement/parameter logging on (it is off by
    default) so the test also proves parameters are hidden there, not just
    in exception text."""
    with caplog.at_level(logging.INFO, logger="sqlalchemy.engine"):
        yield caplog


@pytest.mark.parametrize("target", [engine, lock_engine], ids=["engine", "lock_engine"])
def test_integrity_error_through_engine_hides_bound_parameters(
    target: Engine, sqlalchemy_engine_logs: pytest.LogCaptureFixture
) -> None:
    assert target.hide_parameters is True
    with target.connect() as connection:
        _create_temp_table(connection)
        _insert(connection)
        with pytest.raises(IntegrityError) as excinfo:
            _insert(connection)
        connection.rollback()

    message = str(excinfo.value)
    assert "log_hygiene_probe" in message  # the statement itself is still reported
    assert _HIDDEN_MARKER in message
    assert _SENTINEL not in message
    assert _SENTINEL not in repr(excinfo.value)
    assert _SENTINEL not in sqlalchemy_engine_logs.text
    # Positive control: statement logging really was captured.
    assert "INSERT INTO log_hygiene_probe" in sqlalchemy_engine_logs.text


def test_integrity_error_through_app_session_hides_bound_parameters(
    sqlalchemy_engine_logs: pytest.LogCaptureFixture,
) -> None:
    session = SessionFactory()
    try:
        connection = session.connection()
        _create_temp_table(connection)
        _insert(connection)
        with pytest.raises(IntegrityError) as excinfo:
            _insert(connection)
    finally:
        session.rollback()
        session.close()

    message = str(excinfo.value)
    assert _HIDDEN_MARKER in message
    assert _SENTINEL not in message
    assert _SENTINEL not in sqlalchemy_engine_logs.text
