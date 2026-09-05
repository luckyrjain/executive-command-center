from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

import ecc.main as ecc_main
from ecc.main import app

client = TestClient(app)


def test_liveness() -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Correlation-ID"]


def test_version() -> None:
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json()["version"] == "0.2.0"


def test_readiness_ok() -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_reports_503_on_db_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Architecture review: `/health/ready` is the one health endpoint that
    actually checks the database (unlike `/health/live`, a pure liveness
    check operators are told to depend on for real readiness -- see
    `docs/runbooks/PHASE-1-DEPLOYMENT.md`) but had no test exercising its
    503-on-DB-failure branch. `engine.connect` is monkeypatched to raise,
    mirroring the exact `except Exception` `ready()` itself catches --
    no real DB outage needed to prove the branch works.
    """

    class _FailingEngine:
        @contextmanager
        def connect(self) -> Any:
            raise RuntimeError("simulated database outage")
            yield  # pragma: no cover -- unreachable, makes this a generator

    monkeypatch.setattr(ecc_main, "engine", _FailingEngine())
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
