from __future__ import annotations

from pathlib import Path

import pytest

from Driftwatch.Fix.Sandbox import SandboxRunner


def test_sandbox_runs_in_disposable_checkout(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("original", encoding="utf-8")

    result = SandboxRunner().run(
        tmp_path,
        "tests",
        ["python", "-c", "from pathlib import Path; Path('marker.txt').write_text('changed')"],
    )

    assert result.passed
    assert result.network_allowed is False
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "original"


def test_sandbox_reports_failure_and_network_policy(tmp_path: Path) -> None:
    result = SandboxRunner().run(
        tmp_path,
        "lint",
        ["python", "-c", "import os, sys; print(os.environ['DRIFTWATCH_NETWORK']); sys.exit(3)"],
        allow_network=True,
    )

    assert result.passed is False
    assert result.exit_code == 3
    assert result.stdout.strip() == "allowed"
    assert result.network_allowed is True


def test_sandbox_rejects_empty_commands(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        SandboxRunner().run(tmp_path, "syntax", [])
