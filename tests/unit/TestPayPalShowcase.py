"""End-to-end tests for the PayPal AI Hackathon showcase."""

from __future__ import annotations

from pathlib import Path

from Driftwatch.Services.PayPalShowcase import (
    GroundedRuleModelClient,
    build_evidence_for_delta,
    run_paypal_showcase,
)
from Driftwatch.Surfaces import SurfaceDescriptor
from Driftwatch.Watch.Classifier import EvidenceGroundedClassifier
from Driftwatch.Watch.Differ import Delta


def test_grounded_rule_client_passes_evidence_grounding_validator() -> None:
    delta = Delta(
        surface_ref="api:paypal/op:post_v2_checkout_orders/param:payer",
        change_type="param_deprecated",
        severity="deprecating",
        op="modified",
        before=SurfaceDescriptor(kind="param"),
        after=SurfaceDescriptor(kind="param", deprecated=True),
        detail="payer marked deprecated",
    )
    evidence = build_evidence_for_delta(delta)

    record = EvidenceGroundedClassifier(GroundedRuleModelClient()).classify(delta, evidence)

    assert record.change_type == "param_deprecated"
    assert record.severity == "deprecating"
    assert record.evidence[0].quote == "payer marked deprecated"


def test_paypal_showcase_runs_end_to_end_with_fixture_repo(tmp_path: Path) -> None:
    cache_dir = Path(".cache")
    reviews_dir = tmp_path / "reviews"

    report = run_paypal_showcase(
        cache_dir=cache_dir,
        repo_path=Path("tests/fixtures/paypalRepo"),
        reviews_dir=reviews_dir,
        client_id="your sandbox client id",
        client_secret="your sandbox secret",
        openai_api_key="",
    )

    assert report.sdk_operations >= 40
    assert report.operation_deltas == 6
    assert len(report.classifications) == 6
    assert len(report.findings) >= 4
    assert (reviews_dir / "paypal-findings.json").is_file()
    assert report.sandbox_checkout.amount == "49.00"
