"""Tests for the gate decision function.

This is the component that protects user trust, so its table is tested exhaustively rather
than sampled. Every assertion here corresponds to a promise made in the system design.
"""

from __future__ import annotations

import pytest

from Driftwatch.Match.Resolver import Usage
from Driftwatch.Match.Scorer import Finding, decide


def _finding(change_type: str = "param_renamed", confidence: float = 0.95) -> Finding:
    return Finding(
        change_fingerprint="cf",
        change_type=change_type,
        severity="breaking",
        surface_ref="api:stripe/op:post_v1_charges/param:source",
        usage=Usage(
            surface_ref="api:stripe/op:post_v1_charges/param:source",
            match_kind="param_keyword",
            file_path="app/pay.py",
            start_line=1,
            end_line=1,
            start_byte=0,
            end_byte=10,
            symbol_ref="py:stripe.charges.create",
            snippet="source=tok",
            resolution_confidence=1.0,
        ),
        confidence=confidence,
    )


_READY_KWARGS = {
    "repo_mode": "pr_ready",
    "has_tests": True,
    "tests_passed": True,
    "patch_from_codemod": True,
    "diff_lines": 2,
    "diff_files": 1,
}


def test_high_confidence_verified_codemod_reaches_pr_ready() -> None:
    action, why = decide(_finding(), **_READY_KWARGS)
    assert action == "pr_ready"
    assert "verified" in why


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"tests_passed": False}, "tests did not pass"),
        ({"has_tests": False}, "no tests"),
        ({"patch_from_codemod": False}, "not from a deterministic codemod"),
        ({"diff_lines": 500}, "diff too large"),
        ({"diff_files": 20}, "diff too large"),
    ],
)
def test_each_missing_precondition_demotes_to_draft(override: dict, fragment: str) -> None:
    action, why = decide(_finding(), **{**_READY_KWARGS, **override})
    assert action == "pr_draft"
    assert fragment in why


def test_behavioral_change_never_reaches_a_pull_request() -> None:
    """A changed default needs a human to decide what the right behaviour now is."""
    action, why = decide(_finding("default_changed", confidence=0.99), **_READY_KWARGS)
    assert action == "alert_only"
    assert "never auto-fixed" in why


def test_newly_added_enum_value_is_alert_only() -> None:
    action, _why = decide(_finding("enum_value_added", confidence=0.99), **_READY_KWARGS)
    assert action == "alert_only"


def test_change_without_a_codemod_degrades_to_an_issue() -> None:
    action, why = decide(_finding("param_removed", confidence=0.95), **_READY_KWARGS)
    assert action == "issue_with_diff"
    assert "no codemod" in why


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.95, "pr_ready"), (0.80, "pr_draft"), (0.60, "issue_with_diff"), (0.30, "alert_only")],
)
def test_confidence_thresholds(confidence: float, expected: str) -> None:
    action, _why = decide(_finding(confidence=confidence), **_READY_KWARGS)
    assert action == expected


@pytest.mark.parametrize("mode", ["alert_only", "issue_with_diff", "pr_draft"])
def test_repo_mode_is_a_hard_ceiling(mode: str) -> None:
    """A repository on probation must never receive a stronger action than it allows."""
    action, why = decide(_finding(), **{**_READY_KWARGS, "repo_mode": mode})
    assert action == mode
    assert "capped by repo mode" in why


def test_off_mode_is_respected() -> None:
    action, _why = decide(_finding(), **{**_READY_KWARGS, "repo_mode": "off"})
    assert action == "off"
