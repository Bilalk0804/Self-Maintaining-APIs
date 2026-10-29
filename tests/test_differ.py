"""Tests for the indexer and differ, including the normalisation that suppresses noise."""

from __future__ import annotations

from driftwatch.surfaces import SurfaceDescriptor, SurfaceIndex
from driftwatch.watch.differ import diff_indexes
from driftwatch.watch.openapi_indexer import build_index


def _index(entries: dict[str, SurfaceDescriptor]) -> SurfaceIndex:
    index = SurfaceIndex(provider="x", revision="r")
    for ref, descriptor in entries.items():
        index.add(ref, descriptor)
    return index


def test_identical_indexes_produce_no_deltas() -> None:
    index = _index({"api:x/op:a": SurfaceDescriptor(kind="op", type_sig="get /a")})
    assert diff_indexes(index, index, include_additive=True) == []


def test_documentation_only_change_is_additive() -> None:
    before = _index({"api:x/op:a": SurfaceDescriptor(kind="op", doc_hash="one")})
    after = _index({"api:x/op:a": SurfaceDescriptor(kind="op", doc_hash="two")})
    assert diff_indexes(before, after) == []
    additive = diff_indexes(before, after, include_additive=True)
    assert [d.change_type for d in additive] == ["doc_changed"]


def test_removal_and_addition_on_same_parent_pairs_into_a_rename() -> None:
    shape = SurfaceDescriptor(kind="param", type_sig="string", required=False)
    before = _index({"api:x/op:a/param:old": shape})
    after = _index({"api:x/op:a/param:new": shape})
    deltas = diff_indexes(before, after)
    assert len(deltas) == 1
    assert deltas[0].change_type == "param_renamed"
    assert deltas[0].renamed_to == "api:x/op:a/param:new"


def test_ambiguous_rename_is_not_paired() -> None:
    """Two identically shaped candidates could pair either way, so neither is guessed."""
    shape = SurfaceDescriptor(kind="param", type_sig="string", required=False)
    before = _index({"api:x/op:a/param:p1": shape, "api:x/op:a/param:p2": shape})
    after = _index({"api:x/op:a/param:q1": shape, "api:x/op:a/param:q2": shape})
    deltas = diff_indexes(before, after)
    assert not any(d.change_type == "param_renamed" for d in deltas)
    assert {d.change_type for d in deltas} == {"param_removed", "param_added_optional"} - {
        "param_added_optional"
    } or {d.change_type for d in deltas} == {"param_removed"}


def test_default_change_is_behavioral_and_never_autofixed() -> None:
    before = _index({"api:x/op:a/param:p": SurfaceDescriptor(kind="param", default=True)})
    after = _index({"api:x/op:a/param:p": SurfaceDescriptor(kind="param", default=False)})
    (delta,) = diff_indexes(before, after)
    assert delta.change_type == "default_changed"
    assert delta.severity == "behavioral"
    assert delta.auto_fixable is False


def test_enum_removal_is_breaking_and_yields_a_code_probe() -> None:
    before = _index({"api:x/op:a/param:p": SurfaceDescriptor(kind="param", enum_values=("a", "b"))})
    after = _index({"api:x/op:a/param:p": SurfaceDescriptor(kind="param", enum_values=("a",))})
    (delta,) = diff_indexes(before, after)
    assert delta.change_type == "enum_value_removed"
    assert delta.severity == "breaking"
    assert "b" in delta.code_probes()


def test_required_parameter_addition_is_breaking() -> None:
    before = _index({"api:x/op:a": SurfaceDescriptor(kind="op")})
    after = _index(
        {
            "api:x/op:a": SurfaceDescriptor(kind="op"),
            "api:x/op:a/param:p": SurfaceDescriptor(kind="param", required=True),
        }
    )
    (delta,) = diff_indexes(before, after)
    assert delta.change_type == "param_added_required"
    assert delta.severity == "breaking"


def test_optional_parameter_addition_is_suppressed_by_default() -> None:
    before = _index({"api:x/op:a": SurfaceDescriptor(kind="op")})
    after = _index(
        {
            "api:x/op:a": SurfaceDescriptor(kind="op"),
            "api:x/op:a/param:p": SurfaceDescriptor(kind="param", required=False),
        }
    )
    assert diff_indexes(before, after) == []
    assert len(diff_indexes(before, after, include_additive=True)) == 1


def test_fingerprint_is_stable_and_distinct() -> None:
    before = _index({"api:x/op:a/param:p": SurfaceDescriptor(kind="param", required=False)})
    after = _index({"api:x/op:a/param:p": SurfaceDescriptor(kind="param", required=True)})
    first = diff_indexes(before, after)
    second = diff_indexes(before, after)
    assert first[0].fingerprint == second[0].fingerprint
    assert first[0].fingerprint != before.revision


def test_indexer_resolves_refs_and_flattens_all_of() -> None:
    doc = {
        "paths": {
            "/v1/things": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ThingCreate"}
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "ThingCreate": {
                    "allOf": [
                        {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                        {"properties": {"b": {"type": "integer"}}},
                    ]
                }
            }
        },
    }
    index = build_index(doc, "x", "r")
    assert index.entries["api:x/op:post_v1_things/param:a"].required is True
    assert index.entries["api:x/op:post_v1_things/param:b"].type_sig == "integer"


def test_indexer_sorts_unions_so_reordering_is_not_a_change() -> None:
    def doc(order: list[str]) -> dict:
        return {
            "components": {
                "schemas": {"T": {"properties": {"f": {"oneOf": [{"type": t} for t in order]}}}}
            }
        }

    a = build_index(doc(["string", "integer"]), "x", "r1")
    b = build_index(doc(["integer", "string"]), "x", "r2")
    assert diff_indexes(a, b, include_additive=True) == []


def test_indexer_survives_cyclic_refs() -> None:
    doc = {
        "components": {
            "schemas": {
                "Node": {"properties": {"next": {"$ref": "#/components/schemas/Node"}}},
            }
        }
    }
    index = build_index(doc, "x", "r")
    assert "api:x/obj:Node" in index.entries
