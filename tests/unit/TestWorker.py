from __future__ import annotations

import pytest

from Driftwatch.Worker import _env_float, _required_env


def test_worker_requires_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIFTWATCH_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DRIFTWATCH_DATABASE_URL"):
        _required_env("DRIFTWATCH_DATABASE_URL")


def test_worker_parses_non_negative_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIFTWATCH_WORKER_IDLE_DELAY", "2.5")

    assert _env_float("DRIFTWATCH_WORKER_IDLE_DELAY", 1.0) == 2.5


@pytest.mark.parametrize("value", ["-1", "not-a-number"])
def test_worker_rejects_invalid_backoff(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DRIFTWATCH_WORKER_ERROR_DELAY", value)

    with pytest.raises(RuntimeError, match="DRIFTWATCH_WORKER_ERROR_DELAY"):
        _env_float("DRIFTWATCH_WORKER_ERROR_DELAY", 1.0)
