"""Stage 3: score a resolved usage against a specific change, and decide what to do.

Confidence is a product of independent factors, each of which can veto on its own by going
to zero. The priors here are launch constants; once outcome feedback exists they are meant to
be recalibrated per change type against observed merge rates, which is what turns this number
from a guess into a measurement.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

from Driftwatch.Match.Resolver import Usage
from Driftwatch.Watch.Differ import NEVER_AUTOFIX, Delta

Action = Literal["off", "alert_only", "issue_with_diff", "pr_draft", "pr_ready"]

#: Per-change-type priors. Renames and model swaps are mechanical and historically safe;
#: removals require human judgement about what the replacement should be.
CHANGE_TYPE_PRIOR: dict[str, float] = {
    "param_renamed": 0.98,
    "response_field_renamed": 0.95,
    "model_deprecated": 0.98,
    "enum_value_removed": 0.85,
    "operation_moved": 0.9,
    "param_added_required": 0.8,
    "param_type_changed": 0.7,
    "param_removed": 0.7,
    "response_field_removed": 0.7,
    "operation_removed": 0.7,
    "operation_deprecated": 0.75,
    "param_deprecated": 0.75,
    "param_became_required": 0.8,
    "default_changed": 0.6,
    "enum_value_added": 0.5,
    "nullability_changed": 0.5,
}

#: How much to trust each kind of code match.
MATCH_KIND_WEIGHT: dict[str, float] = {
    "param_keyword": 1.0,
    "operation_call": 1.0,
    "string_literal": 1.0,
    "version_pin": 1.0,
    "response_attribute": 0.9,
    "param_dict_key": 0.85,
    "raw_http_path": 0.85,
    "response_subscript": 0.85,
    "text_only": 0.35,
}

#: Change types a deterministic codemod can handle today.
CODEMOD_SUPPORTED: frozenset[str] = frozenset(
    {"param_renamed", "response_field_renamed", "model_deprecated", "enum_value_removed"}
)

_PR_READY_MIN = 0.90
_PR_DRAFT_MIN = 0.75
_ISSUE_MIN = 0.50

_ACTION_RANK: dict[Action, int] = {
    "off": 0,
    "alert_only": 1,
    "issue_with_diff": 2,
    "pr_draft": 3,
    "pr_ready": 4,
}


@dataclass(frozen=True, slots=True)
class Finding:
    """A scored, actionable match between one change and one location in code."""

    change_fingerprint: str
    change_type: str
    severity: str
    surface_ref: str
    usage: Usage
    confidence: float
    score_factors: dict[str, float] = field(default_factory=dict)
    renamed_to: str | None = None
    detail: str = ""

    @property
    def fingerprint(self) -> str:
        """Stable identity for this finding, used to avoid duplicate alerts and pull requests.

        Anchored on a normalised snippet rather than a line number, so reformatting the file
        does not manufacture a second finding for the same problem.
        """
        normalised = re.sub(r"\s+", " ", self.usage.snippet).strip()
        anchor = hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]
        basis = "|".join(
            [
                self.change_fingerprint,
                self.usage.file_path,
                self.usage.scope,
                str(self.usage.occurrence),
                self.surface_ref,
                anchor,
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    @property
    def auto_fixable(self) -> bool:
        """Whether a deterministic codemod exists for this change type."""
        return self.change_type in CODEMOD_SUPPORTED and self.change_type not in NEVER_AUTOFIX


def score(
    usage: Usage,
    delta: Delta,
    *,
    version_applicable: bool = True,
    surface_map_confidence: float = 0.95,
) -> Finding | None:
    """Score one usage against one change.

    Args:
        usage: The resolved code reference.
        delta: The upstream change being matched.
        version_applicable: False when the repository pins an SDK version that predates the
            change, which makes the finding irrelevant rather than merely uncertain.
        surface_map_confidence: Confidence of the binding that produced this resolution.

    Returns:
        A :class:`Finding`, or ``None`` when the usage does not concern this change.
    """
    if not _surfaces_match(usage.surface_ref, usage.match_kind, delta):
        return None

    factors = {
        "surface_map": surface_map_confidence,
        "resolution": _resolution_confidence(usage, delta),
        "match_kind": MATCH_KIND_WEIGHT.get(usage.match_kind, 0.5),
        "change_type_prior": CHANGE_TYPE_PRIOR.get(delta.change_type, 0.6),
        "version_applicable": 1.0 if version_applicable else 0.0,
    }
    confidence = 1.0
    for value in factors.values():
        confidence *= value

    return Finding(
        change_fingerprint=delta.fingerprint,
        change_type=delta.change_type,
        severity=delta.severity,
        surface_ref=usage.surface_ref,
        usage=usage,
        confidence=round(confidence, 4),
        score_factors={k: round(v, 4) for k, v in factors.items()},
        renamed_to=delta.renamed_to,
        detail=delta.detail,
    )


#: Usage kinds that represent a call to an operation, rather than one of its parameters.
_CALL_SITE_KINDS = frozenset({"operation_call", "raw_http_path"})

#: Changes that explain, rather than cast doubt on, a parameter missing from the SDK.
_EXPLAINS_SDK_ABSENCE = frozenset({"param_renamed", "param_removed", "enum_value_removed"})


def _resolution_confidence(usage: Usage, delta: Delta) -> float:
    """Return the resolution confidence, accounting for what the change itself explains.

    The resolver docks confidence when a keyword argument is absent from the SDK's parameter
    list, since that usually signals a typo or a mis-resolved call. But when the change under
    consideration is the removal or rename of that very parameter, the absence is the
    expected consequence of the change rather than evidence against the match, so the penalty
    is removed.
    """
    if delta.change_type in _EXPLAINS_SDK_ABSENCE and "absent_from_sdk" in usage.evidence:
        return 1.0
    return usage.resolution_confidence


def _surfaces_match(usage_ref: str, usage_kind: str, delta: Delta) -> bool:
    """Return True when a usage refers to the surface a change concerns.

    An operation-level change (deprecated, removed, moved) concerns the *call site* only. It
    deliberately does not match each keyword argument, because a call passing five arguments
    is one problem to fix, not six, and reporting it six times is how a scanner earns a
    reputation for noise.
    """
    if usage_ref == delta.surface_ref:
        return True
    is_leaf_change = "/param:" in delta.surface_ref or "/field:" in delta.surface_ref
    if not is_leaf_change and usage_kind in _CALL_SITE_KINDS:
        return usage_ref == delta.surface_ref
    return False


def decide(
    finding: Finding,
    *,
    repo_mode: Action = "issue_with_diff",
    has_tests: bool = False,
    tests_passed: bool | None = None,
    patch_from_codemod: bool = False,
    diff_lines: int = 0,
    diff_files: int = 0,
) -> tuple[Action, str]:
    """Decide the strongest action permitted for a finding.

    Never returns an action stronger than ``repo_mode``. Behavioural changes such as a
    changed default never reach a pull request at any confidence, because deciding what the
    correct behaviour now is requires a human.

    Returns:
        The chosen action and a one-line reason, which is recorded and shown in the dashboard.
    """
    if repo_mode == "off":
        return "off", "scanning disabled for this repository"

    if finding.change_type in NEVER_AUTOFIX:
        return _clamp("alert_only", repo_mode, f"{finding.change_type} is never auto-fixed")

    if finding.confidence < _ISSUE_MIN:
        return _clamp("alert_only", repo_mode, f"confidence {finding.confidence:.2f} below 0.50")

    if finding.confidence < _PR_DRAFT_MIN or not finding.auto_fixable:
        reason = (
            f"confidence {finding.confidence:.2f} below 0.75"
            if finding.confidence < _PR_DRAFT_MIN
            else f"no codemod for {finding.change_type}"
        )
        return _clamp("issue_with_diff", repo_mode, reason)

    ready = (
        finding.confidence >= _PR_READY_MIN
        and patch_from_codemod
        and has_tests
        and tests_passed is True
        and diff_lines <= 40
        and diff_files <= 5
    )
    if ready:
        return _clamp("pr_ready", repo_mode, "codemod patch verified by passing tests")

    missing = []
    if finding.confidence < _PR_READY_MIN:
        missing.append(f"confidence {finding.confidence:.2f} below 0.90")
    if not patch_from_codemod:
        missing.append("patch not from a deterministic codemod")
    if not has_tests:
        missing.append("repository has no tests")
    elif tests_passed is not True:
        missing.append("tests did not pass")
    if diff_lines > 40 or diff_files > 5:
        missing.append(f"diff too large ({diff_lines} lines, {diff_files} files)")
    return _clamp("pr_draft", repo_mode, "; ".join(missing) or "draft by default")


def _clamp(action: Action, repo_mode: Action, reason: str) -> tuple[Action, str]:
    """Clamp an action to the repository's configured ceiling."""
    if _ACTION_RANK[action] > _ACTION_RANK[repo_mode]:
        return repo_mode, f"{reason} (capped by repo mode {repo_mode})"
    return action, reason
