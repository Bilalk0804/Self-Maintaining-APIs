from __future__ import annotations

import pytest

from Driftwatch.Poller import _env_float, _env_int, _required_env


def test_poller_requires_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIFTWATCH_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DRIFTWATCH_DATABASE_URL"):
        _required_env("DRIFTWATCH_DATABASE_URL")


def test_poller_parses_positive_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIFTWATCH_POLLER_LEASE_SECONDS", "120")

    assert _env_int("DRIFTWATCH_POLLER_LEASE_SECONDS", 300) == 120


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_poller_rejects_invalid_lease(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DRIFTWATCH_POLLER_LEASE_SECONDS", value)

    with pytest.raises(RuntimeError, match="DRIFTWATCH_POLLER_LEASE_SECONDS"):
        _env_int("DRIFTWATCH_POLLER_LEASE_SECONDS", 300)


def test_poller_rejects_invalid_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIFTWATCH_POLLER_IDLE_DELAY", "invalid")

    with pytest.raises(RuntimeError, match="DRIFTWATCH_POLLER_IDLE_DELAY"):
        _env_float("DRIFTWATCH_POLLER_IDLE_DELAY", 1.0)
