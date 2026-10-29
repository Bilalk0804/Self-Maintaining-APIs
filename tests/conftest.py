"""Shared fixtures.

Tests that need the real Stripe SDK or spec are skipped unless the cache has been populated,
so the fast unit tests still run on a clean checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from driftwatch.mapping.model import SurfaceMap
from driftwatch.mapping.sdk_python import derive_stripe_python

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE = REPO_ROOT / ".cache"
SAMPLE_REPO = REPO_ROOT / "tests" / "fixtures" / "sample_repo"


@pytest.fixture(scope="session")
def stripe_sdk_root() -> Path:
    """Path to a stripe-python checkout, or skip."""
    path = CACHE / "stripe-python"
    if not (path / "stripe").is_dir():
        pytest.skip("stripe-python not cached; run scripts/fetch_fixtures.sh")
    return path


@pytest.fixture(scope="session")
def surface_map(stripe_sdk_root: Path) -> SurfaceMap:
    """A Surface Map derived from the cached SDK, built once per session."""
    return derive_stripe_python(stripe_sdk_root, sdk_version="test")


@pytest.fixture(scope="session")
def spec_paths() -> tuple[Path, Path]:
    """Two cached OpenAPI revisions, or skip."""
    before, after = CACHE / "spec_T0.json", CACHE / "spec_T2.json"
    if not before.is_file() or not after.is_file():
        pytest.skip("spec revisions not cached; run scripts/fetch_fixtures.sh")
    return before, after


@pytest.fixture
def sample_repo() -> Path:
    """The fixture repository containing both true positives and decoys."""
    return SAMPLE_REPO
