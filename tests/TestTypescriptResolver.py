from __future__ import annotations

from Driftwatch.Mapping.Model import SurfaceMap, SurfaceMapEntry
from Driftwatch.Match.TypescriptResolver import resolve_file


def test_resolves_typescript_sdk_call_and_request_key() -> None:
    surface = "api:stripe/op:post_v1_charges"
    surface_map = SurfaceMap(
        provider="stripe",
        language="typescript",
        sdk_package="stripe",
        sdk_version="test",
        entries=[
            SurfaceMapEntry(
                surface_ref=surface,
                symbol_ref="ts:stripe.resources.charges.create",
            )
        ],
        op_params={surface: ["source"]},
    )

    usages = resolve_file(
        b'const result = await stripe.resources.charges.create({ source: token });',
        "src/payments.ts",
        surface_map,
    )

    assert [usage.match_kind for usage in usages] == ["operation_call", "param_dict_key"]
    assert usages[0].surface_ref == surface
    assert usages[1].evidence == ("source",)


def test_ignores_unmapped_typescript_calls() -> None:
    surface_map = SurfaceMap(
        provider="stripe",
        language="typescript",
        sdk_package="stripe",
        sdk_version="test",
        entries=[],
    )

    assert resolve_file(b"billing.charges.create({ source: token });", "x.ts", surface_map) == []
