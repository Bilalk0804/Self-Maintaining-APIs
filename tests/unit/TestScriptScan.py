"""The repository scan reads JavaScript and TypeScript, not only Python."""

from __future__ import annotations

from pathlib import Path

from Driftwatch.Mapping.Model import SurfaceMap, SurfaceMapEntry
from Driftwatch.Match.Engine import scan_repo
from Driftwatch.Surfaces import SurfaceDescriptor
from Driftwatch.Watch.Differ import Delta
from tests.Deltas import REAL_RENAME


def test_scan_finds_a_typescript_checkout_call(tmp_path: Path) -> None:
    surface = "api:stripe/op:post_v1_checkout_sessions"
    surface_map = SurfaceMap(
        provider="stripe",
        language="python",
        sdk_package="stripe",
        sdk_version="test",
        entries=[
            SurfaceMapEntry(
                surface_ref=surface,
                symbol_ref="py:stripe.checkout.sessions.create",
            )
        ],
        op_params={surface: ["payment_method_types", "mode"]},
    )
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"stripe": "^17.0.0"}}',
        encoding="utf-8",
    )
    (tmp_path / "pay.ts").write_text(
        "\n".join(
            [
                'import Stripe from "stripe";',
                'const stripe = new Stripe("sk_test");',
                "export async function pay() {",
                "  return stripe.checkout.sessions.create({",
                "    mode: 'payment',",
                "    payment_method_types: ['card'],",
                "  });",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "decoy.ts").write_text(
        "export function local() {\n"
        "  return billing.checkout.sessions.create({ mode: 'payment' });\n"
        "}\n",
        encoding="utf-8",
    )

    result = scan_repo(tmp_path, [REAL_RENAME], surface_map)

    assert result.skipped_reason is None
    assert result.files_parsed >= 1
    assert {finding.usage.file_path for finding in result.findings} == {"pay.ts"}
    assert any(finding.change_type == "param_renamed" for finding in result.findings)


def test_scan_opens_a_file_that_only_mentions_the_sdk_call(tmp_path: Path) -> None:
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
            )
        ],
        op_params={surface: ["amount", "currency"]},
    )
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"stripe": "^18.0.0"}}',
        encoding="utf-8",
    )
    sheet = tmp_path / "app" / "api" / "payment-sheet+api.ts"
    sheet.parent.mkdir(parents=True)
    sheet.write_text(
        "\n".join(
            [
                'import { stripe } from "@/stripe-server";',
                "export async function POST() {",
                '  return stripe.paymentIntents.create({ currency: "usd" });',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    delta = Delta(
        surface_ref=surface,
        change_type="operation_deprecated",
        severity="deprecating",
        op="modified",
        before=SurfaceDescriptor(kind="op", type_sig="post /v1/payment_intents"),
        after=SurfaceDescriptor(kind="op", type_sig="post /v1/payment_intents", deprecated=True),
        detail="payment intents deprecated",
    )

    result = scan_repo(tmp_path, [delta], surface_map)

    assert result.files_considered == 1
    assert {finding.usage.file_path for finding in result.findings} == {
        "app/api/payment-sheet+api.ts"
    }
    assert result.findings[0].usage.symbol_ref == "py:stripe.payment_intents.create"
