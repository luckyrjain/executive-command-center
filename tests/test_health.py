from fastapi.testclient import TestClient

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
    assert response.json()["version"] == "0.1.0"
