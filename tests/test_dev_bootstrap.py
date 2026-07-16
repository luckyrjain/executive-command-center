from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ecc import dev_bootstrap


def test_bootstrap_is_hidden_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dev_bootstrap,
        "get_settings",
        lambda: SimpleNamespace(environment="production"),
    )

    with pytest.raises(HTTPException) as exc_info:
        dev_bootstrap._require_development()

    assert exc_info.value.status_code == 404


def test_bootstrap_page_uses_fragment_and_strict_response_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dev_bootstrap,
        "get_settings",
        lambda: SimpleNamespace(environment="development"),
    )

    response = dev_bootstrap.bootstrap_page()
    body = response.body.decode()

    assert "location.hash" in body
    assert "history.replaceState" in body
    assert "HttpOnly" not in body
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
