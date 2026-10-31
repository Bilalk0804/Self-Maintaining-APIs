"""Unit tests for surface identifiers and descriptors."""

from __future__ import annotations

import pytest

from Driftwatch.Surfaces import (
    SurfaceDescriptor,
    SurfaceRef,
    field_ref,
    op_ref,
    operation_id,
    param_ref,
)


def test_operation_id_normalises_path_parameters() -> None:
    assert operation_id("POST", "/v1/charges/{charge}/capture") == "post_v1_charges_by_id_capture"


def test_operation_id_is_case_insensitive() -> None:
    assert operation_id("get", "/V1/Charges") == operation_id("GET", "/v1/charges")


def test_ref_builders_round_trip() -> None:
    raw = param_ref("stripe", "POST", "/v1/charges", "source")
    parsed = SurfaceRef.parse(raw)
    assert parsed.provider == "stripe"
    assert parsed.kind == "op"
    assert parsed.subkind == "param"
    assert parsed.subident == "source"
    assert str(parsed) == raw


def test_parent_drops_leaf_component() -> None:
    parsed = SurfaceRef.parse(field_ref("stripe", "charge", "source"))
    assert parsed.parent == "api:stripe/obj:charge"


@pytest.mark.parametrize(
    "raw",
    ["stripe/op:x", "api:/op:x", "api:stripe/op:", "api:stripe/op:x/param:", "api:stripe"],
)
def test_parse_rejects_malformed_refs(raw: str) -> None:
    with pytest.raises(ValueError):
        SurfaceRef.parse(raw)


def test_shape_ignores_documentation() -> None:
    a = SurfaceDescriptor(kind="param", type_sig="string", doc_hash="aaa")
    b = SurfaceDescriptor(kind="param", type_sig="string", doc_hash="bbb")
    assert a.shape() == b.shape()


def test_shape_detects_default_change() -> None:
    a = SurfaceDescriptor(kind="param", type_sig="boolean", default=True)
    b = SurfaceDescriptor(kind="param", type_sig="boolean", default=False)
    assert a.shape() != b.shape()


def test_shape_is_hashable_with_nested_defaults() -> None:
    descriptor = SurfaceDescriptor(kind="param", default={"a": [1, 2], "b": {"c": 3}})
    assert hash(descriptor.shape())


def test_op_ref_is_stable_across_parameter_names() -> None:
    assert op_ref("stripe", "get", "/v1/charges/{charge}") == op_ref(
        "stripe", "get", "/v1/charges/{id}"
    )
