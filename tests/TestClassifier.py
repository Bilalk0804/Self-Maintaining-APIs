from __future__ import annotations

import pytest

from Driftwatch.Surfaces import SurfaceDescriptor
from Driftwatch.Watch.Classifier import ClassificationQuarantined, EvidenceGroundedClassifier
from Driftwatch.Watch.Differ import Delta
from Driftwatch.Watch.Evidence import Evidence


class Client:
    def __init__(self, result: dict) -> None:
        self.result = result

    def classify(self, delta: Delta, evidence: list[Evidence]) -> dict:
        return self.result


def make_delta() -> Delta:
    return Delta(
        surface_ref="api:stripe/op:post_v1_charges/param:source",
        change_type="param_added_required",
        severity="breaking",
        op="modified",
        before=SurfaceDescriptor(kind="param", type_sig="string", required=False),
        after=SurfaceDescriptor(kind="param", type_sig="string", required=True),
        changed_attrs=("required",),
        detail="source is now required",
    )


def make_evidence() -> list[Evidence]:
    return [
        Evidence(
            kind="raw_delta",
            ref="delta-ref",
            quote="source is now required",
        )
    ]


def make_result(**overrides: object) -> dict:
    result = {
        "change_type": "param_added_required",
        "severity": "breaking",
        "surface_refs": [make_delta().surface_ref],
        "title": "Source is now required",
        "summary": "The source parameter is required.",
        "code_probes": ["source"],
        "evidence": [
            {"kind": "raw_delta", "ref": "delta-ref", "quote": "source is now required"}
        ],
    }
    result.update(overrides)
    return result


def test_classifier_accepts_grounded_structured_output() -> None:
    result = EvidenceGroundedClassifier(Client(make_result())).classify(
        make_delta(), make_evidence()
    )

    assert result.change_type == "param_added_required"
    assert result.evidence[0].ref == "delta-ref"


def test_classifier_quarantines_fabricated_quote() -> None:
    with pytest.raises(ClassificationQuarantined, match="not grounded"):
        EvidenceGroundedClassifier(
            Client(make_result(
                evidence=[{"kind": "raw_delta", "ref": "delta-ref", "quote": "invented quote"}]
            ))
        ).classify(make_delta(), make_evidence())


def test_classifier_quarantines_changed_deterministic_type() -> None:
    with pytest.raises(ClassificationQuarantined, match="deterministic type"):
        EvidenceGroundedClassifier(Client(make_result(change_type="param_removed"))).classify(
            make_delta(), make_evidence()
        )


def test_classifier_discards_expired_deadline() -> None:
    result = EvidenceGroundedClassifier(
        Client(make_result(deadline_on="2020-01-01"))
    ).classify(make_delta(), make_evidence())

    assert result.deadline_on is None
