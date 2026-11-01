"""Stage 1: cheap, recall-oriented candidate generation.

Parsing every file in a repository for every change would be wasteful, so a literal text
search narrows the field first. This stage is deliberately over-inclusive: a file it misses
can never produce a finding, while a file it wrongly includes merely costs one parse.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs"}
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".tox",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
}


def source_files(repo_root: Path) -> list[Path]:
    """List candidate source files in a repository, skipping vendored directories."""
    resolved_root = repo_root.resolve()
    out: list[Path] = []
    for path in repo_root.rglob("*"):
        if path.is_symlink() or path.suffix not in _SOURCE_SUFFIXES or not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.resolve().is_relative_to(resolved_root):
            continue
        out.append(path)
    return sorted(out)


def find_candidates(repo_root: Path, probes: list[str]) -> list[Path]:
    """Return files containing at least one probe string.

    Uses ``ripgrep`` when available and falls back to an in-process scan, so behaviour does
    not silently depend on what is installed on the host.
    """
    if not probes:
        return []
    unique = sorted({p for p in probes if len(p) >= 3})
    if not unique:
        return []
    rg = shutil.which("rg")
    if rg is not None:
        hits = _ripgrep(rg, repo_root, unique)
        if hits is not None:
            return hits
    return _python_scan(repo_root, unique)


def _ripgrep(rg: str, repo_root: Path, probes: list[str]) -> list[Path] | None:
    """Run ripgrep with one fixed-string pattern per probe."""
    cmd = [rg, "--files-with-matches", "--fixed-strings", "--no-messages"]
    for suffix in sorted(_SOURCE_SUFFIXES):
        cmd += ["--glob", f"*{suffix}"]
    for directory in sorted(_SKIP_DIRS):
        cmd += ["--glob", f"!{directory}/**"]
    for probe in probes:
        cmd += ["-e", probe]
    cmd += ["--", str(repo_root)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    # ripgrep exits 1 for "no matches", which is a valid empty result, and 2 for real errors.
    if result.returncode not in (0, 1):
        return None
    return sorted(Path(line) for line in result.stdout.splitlines() if line)


def _python_scan(repo_root: Path, probes: list[str]) -> list[Path]:
    """In-process fallback for when ripgrep is unavailable."""
    out: list[Path] = []
    for path in source_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(probe in text for probe in probes):
            out.append(path)
    return out
