"""The four spike gates, encoded as regression tests.

These exist so that a later refactor cannot quietly undo a property the architecture depends
on. Each test names the gate it guards.
"""

from __future__ import annotations

from pathlib import Path

from Driftwatch.Mapping.Model import SurfaceMap
from Driftwatch.Match.Engine import scan_repo
from Driftwatch.Watch.Differ import diff_indexes
from Driftwatch.Watch.OpenapiIndexer import build_index, load_openapi
from tests.Deltas import ALL_DELTAS, CHARGE_OP_DEPRECATED


class TestGateA1SurfaceMapDerivation:
    """Gate A1: the endpoint-to-symbol map is derivable from SDK source."""

    def test_derives_hundreds_of_operations(self, surface_map: SurfaceMap) -> None:
        assert surface_map.operation_count >= 300

    def test_binds_all_four_call_styles_for_charges(self, surface_map: SurfaceMap) -> None:
        symbols = {e.symbol_ref for e in surface_map.by_surface()["api:stripe/op:post_v1_charges"]}
        assert "py:stripe.Charge.create" in symbols  # legacy static resource
        assert "py:stripe.ChargeService.create" in symbols  # service class
        assert "py:stripe.v1.charges.create" in symbols  # namespaced client accessor
        assert "py:stripe.charges.create" in symbols  # top-level client alias

    def test_same_named_services_are_not_conflated(self, surface_map: SurfaceMap) -> None:
        """Three classes are named SessionService; bare-name keying would cross-bind them."""
        by_surface = surface_map.by_surface()
        checkout = {e.symbol_ref for e in by_surface["api:stripe/op:post_v1_checkout_sessions"]}
        assert "py:stripe.checkout.sessions.create" in checkout
        assert "py:stripe.financial_connections.sessions.create" not in checkout
        assert "py:stripe.billing_portal.sessions.create" not in checkout

    def test_parameter_sets_resolve_to_the_right_namespace(self, surface_map: SurfaceMap) -> None:
        checkout = surface_map.params_for("api:stripe/op:post_v1_checkout_sessions")
        financial = surface_map.params_for("api:stripe/op:post_v1_financial_connections_sessions")
        assert "line_items" in checkout
        # account_holder is exclusive to financial_connections sessions, so its presence in
        # the checkout parameter set would prove the two TypedDicts had been conflated.
        assert "account_holder" in financial
        assert "account_holder" not in checkout

    def test_response_object_is_recovered(self, surface_map: SurfaceMap) -> None:
        assert surface_map.returns_for("api:stripe/op:post_v1_charges") == "charge"

    def test_no_malformed_v1_v2_paths(self, surface_map: SurfaceMap) -> None:
        """v2 resources carry their own version prefix and must not nest under /v1."""
        assert not [e for e in surface_map.entries if "/op:get_v1_v2" in e.surface_ref]

    def test_every_derived_v1_operation_exists_in_the_spec(
        self, surface_map: SurfaceMap, spec_paths: tuple[Path, Path]
    ) -> None:
        """Cross-validation: derived bindings are checked against the provider's own spec."""
        _before, after = spec_paths
        index = build_index(load_openapi(after.read_bytes()), "stripe", "validation")
        spec_ops = {r for r in index.entries if "/op:" in r and "/param:" not in r}
        derived = {e.surface_ref for e in surface_map.entries}
        # The spec documents only v1; v2 operations are legitimately absent from it.
        derived_v1 = {r for r in derived if "/op:get_v2" not in r and "/op:post_v2" not in r}
        derived_v1 = {r for r in derived_v1 if "/op:delete_v2" not in r}
        assert derived_v1 <= spec_ops


class TestGateA2SpecDiffing:
    """Gate A2: semantic diffing of a large spec is fast and free of phantom changes."""

    def test_adjacent_revisions_produce_few_changes(self, spec_paths: tuple[Path, Path]) -> None:
        before, after = spec_paths
        index_before = build_index(load_openapi(before.read_bytes()), "stripe", "a")
        index_after = build_index(load_openapi(after.read_bytes()), "stripe", "b")
        deltas = diff_indexes(index_before, index_after)
        # A month of real upstream change is dozens of items, not thousands.
        assert 0 < len(deltas) < 300

    def test_additive_changes_dominate_and_are_suppressed(
        self, spec_paths: tuple[Path, Path]
    ) -> None:
        before, after = spec_paths
        a = build_index(load_openapi(before.read_bytes()), "stripe", "a")
        b = build_index(load_openapi(after.read_bytes()), "stripe", "b")
        material = len(diff_indexes(a, b))
        everything = len(diff_indexes(a, b, include_additive=True))
        assert material < everything / 2

    def test_index_is_large_enough_to_be_useful(self, spec_paths: tuple[Path, Path]) -> None:
        _before, after = spec_paths
        index = build_index(load_openapi(after.read_bytes()), "stripe", "b")
        assert len(index) > 5000
        assert len(index.operations()) > 400


class TestGateA3Matching:
    """Gate A3: true positives are found and near-miss decoys are rejected."""

    def test_rejects_every_decoy(self, sample_repo: Path, surface_map: SurfaceMap) -> None:
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        leaks = [f for f in result.findings if "decoy" in f.usage.file_path]
        assert leaks == [], f"decoy leaked: {[f.surface_ref for f in leaks]}"

    def test_finds_the_real_renamed_parameter(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        hits = [f for f in result.findings if f.change_type == "param_renamed"]
        assert len(hits) == 1
        assert hits[0].usage.file_path == "app/Checkout.py"
        assert hits[0].usage.match_kind == "param_keyword"

    def test_finds_both_model_literal_occurrences(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        """Two identical literals in different functions are two findings, not one."""
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        hits = [f for f in result.findings if f.change_type == "model_deprecated"]
        assert len(hits) == 2
        assert len({f.fingerprint for f in hits}) == 2

    def test_resolves_client_legacy_and_raw_http_styles(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        files = {f.usage.file_path for f in result.findings}
        assert {"app/payments.py", "app/legacy.py", "app/raw_http.py"} <= files

    def test_resolves_a_response_field_read(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        hits = [f for f in result.findings if f.usage.match_kind == "response_attribute"]
        # payments.py reads charge.source on two separate lines; both need editing, so both
        # are reported, and they must carry distinct fingerprints.
        assert {f.surface_ref for f in hits} == {"api:stripe/obj:charge/field:source"}
        assert len(hits) == 2
        assert len({f.fingerprint for f in hits}) == 2

    def test_operation_change_reports_one_finding_per_call_site(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        """A call passing five arguments is one problem, not six."""
        result = scan_repo(sample_repo, [CHARGE_OP_DEPRECATED], surface_map)
        per_file: dict[str, int] = {}
        for finding in result.findings:
            per_file[finding.usage.file_path] = per_file.get(finding.usage.file_path, 0) + 1
        assert all(count == 1 for count in per_file.values()), per_file

    def test_skips_repositories_without_the_dependency(
        self, tmp_path: Path, surface_map: SurfaceMap
    ) -> None:
        (tmp_path / "requirements.txt").write_text("flask==3.0.3\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
        result = scan_repo(tmp_path, ALL_DELTAS, surface_map)
        assert result.skipped_reason == "no dependency on stripe"
        assert result.findings == []

    def test_detects_pinned_version_and_test_runner(
        self, sample_repo: Path, surface_map: SurfaceMap
    ) -> None:
        result = scan_repo(sample_repo, ALL_DELTAS, surface_map)
        stripe_dep = next(
            d for d in result.dependencies if d.package_name == "stripe" and not d.is_raw_http
        )
        assert stripe_dep.version_spec == ">=11.0,<13.0"
        assert result.test_runner == "pytest"
        assert any(d.is_raw_http for d in result.dependencies)
