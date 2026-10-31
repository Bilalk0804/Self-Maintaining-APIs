"""Run patch verification commands in an isolated temporary checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SandboxPhase = Literal["syntax", "typecheck", "lint", "tests"]


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Structured result from one verification command."""

    phase: str
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    network_allowed: bool

    @property
    def passed(self) -> bool:
        """Whether the command completed successfully."""
        return self.exit_code == 0 and not self.timed_out


class SandboxRunner:
    """Verify commands against a disposable copy of a repository."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        repo_root: Path,
        phase: SandboxPhase | str,
        command: list[str],
        allow_network: bool = False,
    ) -> SandboxResult:
        """Run a command in a temporary checkout and capture its complete result.

        Network access is declared explicitly through the environment variable
        ``DRIFTWATCH_NETWORK``. The runner never silently upgrades a disabled request.
        """
        if not command:
            raise ValueError("sandbox command must not be empty")
        if not repo_root.is_dir():
            raise FileNotFoundError(f"sandbox repository does not exist: {repo_root}")

        with tempfile.TemporaryDirectory(prefix="driftwatch-sandbox-") as directory:
            checkout = Path(directory) / "repo"
            shutil.copytree(
                repo_root,
                checkout,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "venv",
                    "__pycache__",
                    ".pytest_cache",
                    "node_modules",
                ),
            )
            environment = os.environ.copy()
            environment["DRIFTWATCH_NETWORK"] = "allowed" if allow_network else "disabled"
            try:
                completed = subprocess.run(
                    command,
                    cwd=checkout,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return SandboxResult(
                    phase=str(phase),
                    command=tuple(command),
                    exit_code=124,
                    stdout=_output(exc.stdout),
                    stderr=_output(exc.stderr),
                    timed_out=True,
                    network_allowed=allow_network,
                )
        return SandboxResult(
            phase=str(phase),
            command=tuple(command),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
            network_allowed=allow_network,
        )


def _output(value: str | bytes | None) -> str:
    """Normalize timeout output across Python subprocess implementations."""
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
