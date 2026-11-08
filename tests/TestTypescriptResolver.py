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
        b"const result = await stripe.resources.charges.create({ source: token });",
        "src/payments.ts",
        surface_map,
    )

    assert [usage.match_kind for usage in usages] == ["operation_call", "param_dict_key"]
    assert usages[0].surface_ref == surface
    assert usages[1].evidence == ("source",)


def test_matches_node_camel_case_against_python_symbols() -> None:
    surface = "api:stripe/op:post_v1_payment_intents"
    surface_map = SurfaceMap(
        provider="stripe",
        language="python",
        sdk_package="stripe",
        sdk_version="test",
        entries=[
            SurfaceMapEntry(
                surface_ref=surface,
                symbol_ref="py:stripe.payment_intents.create",
            ),
            SurfaceMapEntry(
                surface_ref=surface,
                symbol_ref="py:stripe.v1.payment_intents.create_async",
            ),
        ],
        op_params={surface: ["amount", "currency"]},
    )
    source = (
        b'import Stripe from "stripe";\n'
        b'const stripe = new Stripe("sk_test");\n'
        b'await stripe.paymentIntents.create({ amount: 100, currency: "usd" });\n'
    )

    usages = resolve_file(source, "pay.ts", surface_map)

    assert usages[0].symbol_ref == "py:stripe.payment_intents.create"
    assert usages[0].surface_ref == surface
    assert {usage.surface_ref for usage in usages if usage.match_kind == "param_dict_key"} == {
        f"{surface}/param:amount",
        f"{surface}/param:currency",
    }


def test_ignores_a_local_object_with_the_same_method_names() -> None:
    surface_map = SurfaceMap(
        provider="stripe",
        language="python",
        sdk_package="stripe",
        sdk_version="test",
        entries=[
            SurfaceMapEntry(
                surface_ref="api:stripe/op:post_v1_payment_intents",
                symbol_ref="py:stripe.payment_intents.create",
            )
        ],
    )

    usages = resolve_file(
        b"billing.paymentIntents.create({ amount: 100 });",
        "decoy.ts",
        surface_map,
    )

    assert usages == []


def test_binds_a_client_loaded_from_a_local_stripe_module() -> None:
    surface = "api:stripe/op:post_v1_customers"
    surface_map = SurfaceMap(
        provider="stripe",
        language="python",
        sdk_package="stripe",
        sdk_version="test",
        entries=[SurfaceMapEntry(surface_ref=surface, symbol_ref="py:stripe.customers.create")],
        op_params={surface: ["email"]},
    )
    source = (
        b"const stripeAPI = (await import('../payment/stripe.js')).default;\n"
        b"await stripeAPI.customers.create({ email: receiptEmail });\n"
    )

    usages = resolve_file(source, "controller.ts", surface_map)

    assert [usage.match_kind for usage in usages] == ["operation_call", "param_dict_key"]
    assert usages[0].symbol_ref == "py:stripe.customers.create"


def test_binds_a_class_field_assigned_from_the_constructor() -> None:
    surface = "api:stripe/op:post_v1_checkout_sessions"
    surface_map = _stripe_map(surface, "py:stripe.checkout.sessions.create", ["ui_mode"])
    source = (
        b'import Stripe from "stripe";\n'
        b"class Pay {\n"
        b"  private stripe: Stripe | null = null;\n"
        b"  constructor(key: string) { this.stripe = new Stripe(key); }\n"
        b"  async checkout() {\n"
        b'    return this.stripe.checkout.sessions.create({ ui_mode: "embedded" });\n'
        b"  }\n"
        b"}\n"
    )

    usages = resolve_file(source, "pay.ts", surface_map)

    assert usages[0].symbol_ref == "py:stripe.checkout.sessions.create"
    assert any(usage.surface_ref == f"{surface}/param:ui_mode" for usage in usages)


def test_binds_a_factory_that_returns_the_constructed_client() -> None:
    surface = "api:stripe/op:post_v1_payment_intents"
    surface_map = _stripe_map(surface, "py:stripe.payment_intents.create", ["amount"])
    source = (
        b'import Stripe from "stripe";\n'
        b"let stripe: Stripe | null = null;\n"
        b"function getStripe() {\n"
        b'  if (!stripe) stripe = new Stripe("sk_test");\n'
        b"  return stripe;\n"
        b"}\n"
        b"export async function pay() {\n"
        b"  return getStripe().paymentIntents.create({ amount: 100 });\n"
        b"}\n"
    )

    usages = resolve_file(source, "pay.ts", surface_map)

    assert usages[0].symbol_ref == "py:stripe.payment_intents.create"
    assert usages[0].match_kind == "operation_call"


def test_binds_a_factory_imported_from_a_local_stripe_module() -> None:
    surface = "api:stripe/op:post_v1_customers"
    surface_map = _stripe_map(surface, "py:stripe.customers.create", ["email"])
    source = (
        b'import { getStripe } from "@/lib/billing/stripe";\n'
        b'await getStripe().customers.create({ email: "a@b.c" });\n'
    )

    usages = resolve_file(source, "checkout.ts", surface_map)

    assert [usage.match_kind for usage in usages] == ["operation_call", "param_dict_key"]
    assert usages[0].symbol_ref == "py:stripe.customers.create"


def test_ignores_a_factory_imported_from_an_unrelated_module() -> None:
    surface_map = _stripe_map(
        "api:stripe/op:post_v1_customers",
        "py:stripe.customers.create",
        ["email"],
    )
    source = (
        b'import { getStripe } from "@/lib/billing/payments";\n'
        b'await getStripe().customers.create({ email: "a@b.c" });\n'
    )

    assert resolve_file(source, "checkout.ts", surface_map) == []


def test_ignores_a_class_field_that_is_not_the_sdk() -> None:
    surface_map = _stripe_map(
        "api:stripe/op:post_v1_checkout_sessions",
        "py:stripe.checkout.sessions.create",
        [],
    )
    source = (
        b"class Pay {\n"
        b"  stripe = { checkout: { sessions: { create(_opts: object) {} } } };\n"
        b"  checkout() { return this.stripe.checkout.sessions.create({}); }\n"
        b"}\n"
    )

    assert resolve_file(source, "decoy.ts", surface_map) == []


def _stripe_map(surface: str, symbol: str, params: list[str]) -> SurfaceMap:
    return SurfaceMap(
        provider="stripe",
        language="python",
        sdk_package="stripe",
        sdk_version="test",
        entries=[SurfaceMapEntry(surface_ref=surface, symbol_ref=symbol)],
        op_params={surface: params},
    )


def test_ignores_unmapped_typescript_calls() -> None:
    surface_map = SurfaceMap(
        provider="stripe",
        language="typescript",
        sdk_package="stripe",
        sdk_version="test",
        entries=[],
    )

    assert resolve_file(b"billing.charges.create({ source: token });", "x.ts", surface_map) == []
