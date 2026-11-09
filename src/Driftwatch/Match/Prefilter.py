"""Stage 0: decide whether a repository can possibly be affected.

This stage exists for cost, not correctness. Most repositories do not depend on most
providers, so resolving the dependency manifests first lets the scanner skip the overwhelming
majority of (repo, provider) pairs before any file is parsed.

It also recovers the pinned version, which later tells us whether a change even applies to
the version in use, and detects raw-HTTP integrations that never import an SDK.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: Provider slug -> (pypi packages, npm packages, api hostnames).
PROVIDER_PACKAGES: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    "stripe": (frozenset({"stripe"}), frozenset({"stripe"}), frozenset({"api.stripe.com"})),
    "openai": (
        frozenset({"openai"}),
        frozenset({"openai"}),
        frozenset({"api.openai.com"}),
    ),
    "razorpay": (
        frozenset({"razorpay"}),
        frozenset({"razorpay"}),
        frozenset({"api.razorpay.com"}),
    ),
    "paypal": (
        frozenset({"paypal-server-sdk", "paypalserversdk"}),
        frozenset({"paypal-server-sdk", "@paypal/paypal-server-sdk"}),
        frozenset({"api-m.paypal.com", "api.paypal.com", "api-m.sandbox.paypal.com"}),
    ),
}

_PY_MANIFESTS = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile")
_JS_MANIFESTS = ("package.json",)

#: Matches ``stripe>=11.0,<13.0`` / ``stripe==1.2.3`` / ``stripe[extra]~=4``.
_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(?P<spec>[<>=!~][^#;]*)?",
)


@dataclass(frozen=True, slots=True)
class RepoDependency:
    """One detected dependency on a provider."""

    provider: str
    ecosystem: str
    package_name: str
    version_spec: str | None
    manifest_path: str
    is_raw_http: bool = False


def scan_dependencies(repo_root: Path) -> list[RepoDependency]:
    """Detect provider dependencies declared anywhere in a repository.

    Args:
        repo_root: Repository checkout root.

    Returns:
        One entry per (provider, manifest) pair discovered.
    """
    found: list[RepoDependency] = []
    for manifest in _iter_manifests(repo_root):
        rel = manifest.relative_to(repo_root).as_posix()
        try:
            declared = _parse_manifest(manifest)
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            continue
        ecosystem = "npm" if manifest.name in _JS_MANIFESTS else "pypi"
        for name, spec in declared.items():
            provider = _provider_for(name, ecosystem)
            if provider is not None:
                found.append(
                    RepoDependency(
                        provider=provider,
                        ecosystem=ecosystem,
                        package_name=name,
                        version_spec=spec,
                        manifest_path=rel,
                    )
                )
    return found


def detect_raw_http(repo_root: Path, source_files: list[Path]) -> list[RepoDependency]:
    """Detect providers called over raw HTTP with no SDK dependency.

    Plenty of integrations skip the SDK entirely, which matters most for providers whose SDK
    is thin. Without this, those repositories look unaffected by every change.
    """
    hostnames = {
        host: provider for provider, (_, _, hosts) in PROVIDER_PACKAGES.items() for host in hosts
    }
    out: list[RepoDependency] = []
    seen: set[str] = set()
    for path in source_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for host, provider in hostnames.items():
            if host in text and provider not in seen:
                seen.add(provider)
                out.append(
                    RepoDependency(
                        provider=provider,
                        ecosystem="http",
                        package_name=host,
                        version_spec=None,
                        manifest_path=path.relative_to(repo_root).as_posix(),
                        is_raw_http=True,
                    )
                )
    return out


def detect_test_runner(repo_root: Path) -> str | None:
    """Return the repository's test runner, if one can be identified.

    The answer gates automated pull requests: without a test suite there is nothing to verify
    a patch against, so a finding cannot be promoted past a draft.
    """
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        if "pytest" in str(data.get("tool", {})):
            return "pytest"
    if (repo_root / "pytest.ini").is_file() or (repo_root / "tox.ini").is_file():
        return "pytest"
    package_json = repo_root / "package.json"
    if package_json.is_file():
        text = package_json.read_text(encoding="utf-8", errors="ignore")
        for runner in ("vitest", "jest"):
            if runner in text:
                return runner
    if any((repo_root / d).is_dir() for d in ("tests", "test")):
        return "pytest"
    return None


def _iter_manifests(repo_root: Path):
    """Yield dependency manifests, skipping vendored and virtual-env directories."""
    skip = {"node_modules", ".venv", "venv", ".git", "site-packages", "vendor", ".tox"}
    for name in (*_PY_MANIFESTS, *_JS_MANIFESTS):
        for path in repo_root.rglob(name):
            if not any(part in skip for part in path.parts):
                yield path


def _parse_manifest(manifest: Path) -> dict[str, str | None]:
    """Return ``{package_name: version_spec}`` declared by one manifest."""
    text = manifest.read_text(encoding="utf-8", errors="ignore")
    if manifest.name == "package.json":
        import json

        data = json.loads(text)
        out: dict[str, str | None] = {}
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            for name, spec in (data.get(section) or {}).items():
                out[name] = spec if isinstance(spec, str) else None
        return out

    if manifest.name == "pyproject.toml":
        data = tomllib.loads(text)
        specs: list[str] = list(data.get("project", {}).get("dependencies") or [])
        for group in (data.get("dependency-groups") or {}).values():
            specs.extend(s for s in group if isinstance(s, str))
        poetry = data.get("tool", {}).get("poetry", {}).get("dependencies") or {}
        out = {name: (spec if isinstance(spec, str) else None) for name, spec in poetry.items()}
        out.update(_parse_requirement_lines(specs))
        return out

    return _parse_requirement_lines(text.splitlines())


def _parse_requirement_lines(lines: list[str]) -> dict[str, str | None]:
    """Parse PEP 508-ish requirement strings, ignoring comments and options."""
    out: dict[str, str | None] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = _REQ_RE.match(stripped)
        if match is None:
            continue
        spec = match.group("spec")
        out[match.group("name").lower()] = spec.strip() if spec else None
    return out


def _provider_for(package_name: str, ecosystem: str) -> str | None:
    """Return the provider a package belongs to, if any."""
    name = package_name.lower()
    for provider, (pypi, npm, _hosts) in PROVIDER_PACKAGES.items():
        if name in (pypi if ecosystem == "pypi" else npm):
            return provider
    return None
