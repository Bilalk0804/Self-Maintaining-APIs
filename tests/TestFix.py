"""Gate A4 and splice safety: patches must be minimal, correct, and never silently wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from Driftwatch.Fix import Codemods as codemods_mod
from Driftwatch.Fix.Splice import (
    Edit,
    SpliceError,
    apply_edits,
    diff_stats,
    unified_diff,
    verify_patch,
)
from Driftwatch.Mapping.Model import SurfaceMap
from Driftwatch.Match.Engine import scan_repo
from Driftwatch.Match.Resolver import Usage
from Driftwatch.Match.Scorer import Finding, decide
from Driftwatch.Tsutil import has_errors, parse
from tests.Deltas import ALL_DELTAS, REAL_RENAME


def test_apply_edits_replaces_a_single_range() -> None:
    src = b"f(alpha=1, beta=2)"
    edits = [Edit(2, 7, b"omega", "rename")]
    assert apply_edits(src, edits) == b"f(omega=1, beta=2)"


def test_apply_edits_handles_multiple_edits_in_any_order() -> None:
    src = b"f(a=1, b=2)"
    edits = [Edit(7, 8, b"bee", "r2"), Edit(2, 3, b"ay", "r1")]
    assert apply_edits(src, edits) == b"f(ay=1, bee=2)"


def test_apply_edits_rejects_overlaps() -> None:
    with pytest.raises(SpliceError, match="overlapping"):
        apply_edits(b"abcdef", [Edit(0, 3, b"x", "a"), Edit(2, 5, b"y", "b")])


def test_apply_edits_rejects_out_of_range() -> None:
    with pytest.raises(SpliceError, match="file is"):
        apply_edits(b"abc", [Edit(1, 99, b"x", "a")])


def test_verify_patch_rejects_broken_syntax() -> None:
    with pytest.raises(SpliceError, match="does not parse"):
        verify_patch(b"def f(:\n", expected_edits=1)


def test_diff_stats_counts_changed_lines_only() -> None:
    diff = unified_diff(b"a\nb\nc\n", b"a\nB\nc\n", "f.py")
    assert diff_stats(diff) == (1, 1)


def test_insert_required_keyword_argument_is_explicitly_configured() -> None:
    source = b"client.create(amount=10)"
    usage = Usage(
        surface_ref="api:stripe/op:post_v1_charges",
        match_kind="operation_call",
        file_path="payments.py",
        start_line=1,
        end_line=1,
        start_byte=0,
        end_byte=len(source),
        symbol_ref="stripe.Charges.create",
        snippet=source.decode(),
        resolution_confidence=0.95,
    )
    finding = Finding(
        change_fingerprint="change",
        change_type="param_added_required",
        severity="breaking",
        surface_ref=usage.surface_ref,
        usage=usage,
        confidence=0.9,
    )
    name, edits = codemods_mod.plan_edits(
        finding,
        source,
        codemods_mod.default_registry(
            required_parameters={usage.surface_ref: "source=token"}
        ),
    )

    assert name == "insert_required_keyword_argument"
    assert apply_edits(source, edits) == b"client.create(amount=10, source=token)"


class TestGateA4ByteSplicePatches:
    """Gate A4: a codemod's diff touches only the intended bytes."""

    def test_rename_touches_only_the_keyword_name(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        result = scan_repo(sample_repo, [REAL_RENAME], surface_map)
        finding = next(f for f in result.findings if f.change_type == "param_renamed")
        path = sample_repo / finding.usage.file_path
        original = path.read_bytes()

        name, edits = codemods_mod.plan_edits(finding, original, codemods_mod.default_registry())
        assert name == "rename_keyword_argument"
        assert len(edits) == 1

        patched = apply_edits(original, edits)
        verify_patch(patched, len(edits))

        # Exactly one line differs, and only the parameter name within it.
        before_lines = original.decode().splitlines()
        after_lines = patched.decode().splitlines()
        assert len(before_lines) == len(after_lines)
        changed = [
            i for i, (a, b) in enumerate(zip(before_lines, after_lines, strict=True)) if a != b
        ]
        assert len(changed) == 1
        assert "payment_method_types=" in before_lines[changed[0]]
        assert "allowed_payment_method_types=" in after_lines[changed[0]]
        # The argument's value is preserved byte for byte.
        assert '["card", "link"]' in after_lines[changed[0]]

    def test_patched_file_still_parses(self, sample_repo: Path, surface_map: SurfaceMap) -> None:
        result = scan_repo(sample_repo, [REAL_RENAME], surface_map)
        finding = next(f for f in result.findings if f.change_type == "param_renamed")
        path = sample_repo / finding.usage.file_path
        _name, edits = codemods_mod.plan_edits(
            finding, path.read_bytes(), codemods_mod.default_registry()
        )
        patched = apply_edits(path.read_bytes(), edits)
        assert not has_errors(parse(patched, "python"))

    def test_diff_is_small_enough_to_review(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        result = scan_repo(sample_repo, [REAL_RENAME], surface_map)
        finding = next(f for f in result.findings if f.change_type == "param_renamed")
        path = sample_repo / finding.usage.file_path
        original = path.read_bytes()
        _name, edits = codemods_mod.plan_edits(finding, original, codemods_mod.default_registry())
        diff = unified_diff(original, apply_edits(original, edits), finding.usage.file_path)
        added, removed = diff_stats(diff)
        assert (added, removed) == (1, 1)

    def test_model_swap_preserves_quote_style(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        findings = [f for f in result.findings if f.change_type == "model_deprecated"]
        registry = codemods_mod.default_registry({"gpt-4-vision-preview": "gpt-4o"})
        path = sample_repo / findings[0].usage.file_path
        original = path.read_bytes()
        all_edits = []
        for finding in findings:
            _name, edits = codemods_mod.plan_edits(finding, original, registry)
            all_edits.extend(edits)
        assert len(all_edits) == 2
        patched = apply_edits(original, all_edits)
        verify_patch(patched, len(all_edits))
        assert b'"gpt-4o"' in patched
        assert b"gpt-4-vision-preview" not in patched.replace(
            b'# Historically we used "gpt-4-vision-preview"', b""
        )

    def test_no_codemod_for_a_removal_is_a_normal_outcome(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        """Removals have no safe mechanical fix, so they must degrade to an issue."""
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        finding = next(f for f in result.findings if f.change_type == "param_removed")
        path = sample_repo / finding.usage.file_path
        name, edits = codemods_mod.plan_edits(
            finding, path.read_bytes(), codemods_mod.default_registry()
        )
        assert (name, edits) == ("", [])
        action, _why = decide(finding, repo_mode="pr_ready", has_tests=True, tests_passed=True)
        assert action in ("issue_with_diff", "alert_only")
