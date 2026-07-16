from __future__ import annotations

import pytest

from scripts import bootstrap_dev


def test_database_url_converts_sqlalchemy_psycopg_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ECC_DATABASE_URL",
        "postgresql+psycopg://ecc:ecc@localhost:5432/ecc",
    )

    assert bootstrap_dev._database_url() == "postgresql://ecc:ecc@localhost:5432/ecc"


def test_database_url_rejects_unsupported_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_DATABASE_URL", "sqlite:///ecc.db")

    with pytest.raises(SystemExit, match="must use postgresql"):
        bootstrap_dev._database_url()


def test_bootstrap_rejects_missing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ECC_ENV", raising=False)

    with pytest.raises(SystemExit, match="ECC_ENV=development"):
        bootstrap_dev._validate_environment("postgresql://ecc:ecc@localhost:5432/ecc")


def test_bootstrap_rejects_non_development_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_ENV", "production")

    with pytest.raises(SystemExit, match="ECC_ENV=development"):
        bootstrap_dev._validate_environment("postgresql://ecc:ecc@localhost:5432/ecc")


def test_bootstrap_rejects_remote_database_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_ENV", "development")
    monkeypatch.delenv("ECC_BOOTSTRAP_ALLOW_REMOTE_DATABASE", raising=False)

    with pytest.raises(SystemExit, match="Refusing to bootstrap a non-local database"):
        bootstrap_dev._validate_environment("postgresql://ecc:ecc@db.example.com:5432/ecc")


def test_bootstrap_allows_explicit_remote_development_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_ENV", "development")
    monkeypatch.setenv("ECC_BOOTSTRAP_ALLOW_REMOTE_DATABASE", "true")

    bootstrap_dev._validate_environment("postgresql://ecc:ecc@dev-db.internal:5432/ecc")
