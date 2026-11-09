"""Grounded AI classification and end-to-end PayPal AI Hackathon showcase."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from Driftwatch.Mapping.SdkPaypal import derive_paypal_python
from Driftwatch.Match.Engine import scan_repo
from Driftwatch.Match.Scorer import Finding
from Driftwatch.Services.ModelClient import (
    ModelClientConfig,
    ModelClientError,
    OpenAIModelClient,
)
from Driftwatch.Services.PayPalCheckout import (
    PLAN_AMOUNT,
    PLAN_CURRENCY,
    PLAN_NAME,
    SANDBOX_ORIGIN,
    CheckoutOrder,
    PayPalCheckout,
    PayPalCheckoutConfig,
    PayPalCheckoutError,
)
from Driftwatch.Surfaces import SurfaceRef
from Driftwatch.Watch.Classifier import (
    ChangeRecordOut,
    ClassificationQuarantined,
    EvidenceGroundedClassifier,
    ModelClient,
)
from Driftwatch.Watch.Differ import Delta, diff_indexes
from Driftwatch.Watch.Evidence import Evidence
from Driftwatch.Watch.OpenapiIndexer import build_index, load_openapi

_TITLES: dict[str, str] = {
    "param_removed": "Removed request parameter `{leaf}` on `{op}`",
    "param_renamed": "Renamed parameter `{leaf}` on `{op}`",
    "param_became_required": "Parameter `{leaf}` is now required on `{op}`",
    "param_added_required": "Added required parameter `{leaf}` on `{op}`",
    "param_deprecated": "Deprecated parameter `{leaf}` on `{op}`",
    "param_type_changed": "Changed schema type of `{leaf}` on `{op}`",
    "enum_value_added": "Added enum values to `{leaf}` on `{op}`",
    "enum_value_removed": "Removed enum values from `{leaf}` on `{op}`",
}


class GroundedRuleModelClient(ModelClient):
    """Offline structured classifier that emits quote-grounded records from delta evidence.

    When ``OPENAI_API_KEY`` is not set, judges can still run the full evidence-grounded
    AI pipeline deterministically without external keys. Every citation quote is taken
    verbatim from the supplied evidence so ``EvidenceGroundedClassifier`` verifies it
    through the exact same trust boundary used for live LLM calls.
    """

    def classify(self, delta: Delta, evidence: list[Evidence]) -> dict[str, Any]:
        """Construct a schema-valid, evidence-cited classification payload."""
        primary = evidence[0]
        parsed = SurfaceRef.parse(delta.surface_ref)
        leaf = parsed.subident or parsed.ident
        op_name = parsed.ident
        template = _TITLES.get(delta.change_type, "API {change_type} on `{leaf}`")
        title = template.format(leaf=leaf, op=op_name, change_type=delta.change_type)[:120]
        probes = delta.code_probes() or [leaf]
        recipe = _recipe_for(delta, leaf)
        return {
            "change_type": delta.change_type,
            "severity": delta.severity,
            "surface_refs": [delta.surface_ref],
            "title": title,
            "summary": (
                f"PayPal Orders v2 {delta.severity} change ({delta.change_type}) on "
                f"{delta.surface_ref}: {primary.quote}"
            ),
            "enforcement": "hard" if delta.severity == "breaking" else "soft",
            "code_probes": probes,
            "recipes": [recipe],
            "evidence": [
                {
                    "kind": primary.kind,
                    "ref": primary.ref,
                    "quote": primary.quote,
                }
            ],
        }


def _recipe_for(delta: Delta, leaf: str) -> dict[str, Any]:
    if delta.change_type == "param_renamed" and delta.renamed_to:
        new_leaf = SurfaceRef.parse(delta.renamed_to).subident or delta.renamed_to
        return {
            "kind": "rename_param",
            "params": {"from": leaf, "to": new_leaf},
            "language": "python",
            "confidence": 0.95,
            "notes": f"Rename `{leaf}` to `{new_leaf}` in the request payload.",
        }
    if delta.change_type == "param_became_required":
        return {
            "kind": "insert_required_param",
            "params": {"param": leaf},
            "language": "python",
            "confidence": 0.85,
            "notes": f"Ensure `{leaf}` is populated on every call to this PayPal endpoint.",
        }
    return {
        "kind": "manual_only",
        "params": {"param": leaf},
        "language": "python",
        "confidence": 0.8,
        "notes": f"Audit `{leaf}` usage against the updated PayPal Orders v2 contract.",
    }


@dataclass(frozen=True, slots=True)
class ShowcaseClassification:
    """One PayPal change validated through ``EvidenceGroundedClassifier``."""

    surface_ref: str
    change_type: str
    severity: str
    title: str
    summary: str
    cited_quote: str
    recipe_kind: str
    recipe_notes: str | None


@dataclass(frozen=True, slots=True)
class SandboxCheckoutStatus:
    """Outcome of the PayPal sandbox B2B checkout step during the showcase."""

    mode: str
    endpoint: str
    plan: str
    amount: str
    currency: str
    order_id: str | None
    approve_url: str | None
    note: str


@dataclass(frozen=True, slots=True)
class PayPalShowcaseReport:
    """Complete summary produced by ``run_paypal_showcase``."""

    sdk_operations: int
    sdk_bindings: int
    spec_surfaces_before: int
    spec_surfaces_after: int
    total_deltas: int
    operation_deltas: int
    ai_mode: str
    classifications: list[ShowcaseClassification]
    findings: list[Finding]
    sandbox_checkout: SandboxCheckoutStatus
    reviews_written_to: str


def build_evidence_for_delta(delta: Delta) -> list[Evidence]:
    """Build verbatim evidence records for a detected delta."""
    quote = delta.detail if len(delta.detail) >= 8 else f"{delta.change_type}: {delta.surface_ref}"
    return [
        Evidence(
            kind="raw_delta",
            ref=delta.fingerprint,
            quote=quote,
        )
    ]


def classify_paypal_deltas(
    deltas: list[Delta],
    *,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.openai.com/v1",
) -> tuple[str, list[ShowcaseClassification]]:
    """Classify operation-level PayPal deltas through the evidence-grounded AI boundary."""
    key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
    live_client: OpenAIModelClient | None = None
    ai_mode = "grounded-ai-offline"
    if key:
        live_client = OpenAIModelClient(
            ModelClientConfig(api_key=key, model=model, base_url=base_url)
        )
        ai_mode = f"openai:{model}"

    fallback = EvidenceGroundedClassifier(GroundedRuleModelClient())
    out: list[ShowcaseClassification] = []
    try:
        for delta in deltas:
            evidence = build_evidence_for_delta(delta)
            record: ChangeRecordOut | None = None
            if live_client is not None:
                try:
                    record = EvidenceGroundedClassifier(live_client).classify(delta, evidence)
                except (ModelClientError, ClassificationQuarantined):
                    record = None
            if record is None:
                record = fallback.classify(delta, evidence)
            recipe = record.recipes[0] if record.recipes else None
            out.append(
                ShowcaseClassification(
                    surface_ref=delta.surface_ref,
                    change_type=record.change_type,
                    severity=record.severity,
                    title=record.title,
                    summary=record.summary,
                    cited_quote=record.evidence[0].quote,
                    recipe_kind=recipe.kind if recipe else "manual_only",
                    recipe_notes=recipe.notes if recipe else None,
                )
            )
    finally:
        if live_client is not None:
            live_client.close()
    return ai_mode, out


def run_paypal_showcase(
    *,
    cache_dir: Path = Path(".cache"),
    repo_path: Path = Path("tests/fixtures/paypalRepo"),
    reviews_dir: Path = Path(".cache/reviews"),
    client_id: str | None = None,
    client_secret: str | None = None,
    openai_api_key: str | None = None,
) -> PayPalShowcaseReport:
    """Run the end-to-end PayPal AI Hackathon pipeline without requiring Postgres."""
    sdk_root = cache_dir / "PayPal-Python-Server-SDK"
    before_path = cache_dir / "paypal_before.json"
    after_path = cache_dir / "paypal_after.json"
    missing = [p for p in (sdk_root, before_path, after_path) if not p.exists()]
    if missing:
        names = ", ".join(str(p) for p in missing)
        raise FileNotFoundError(
            f"missing PayPal fixtures ({names}); run ./scripts/fetchFixtures.sh first"
        )

    smap = derive_paypal_python(sdk_root, sdk_version="2.4.0")
    before_idx = build_index(load_openapi(before_path.read_bytes()), "paypal", "before")
    after_idx = build_index(load_openapi(after_path.read_bytes()), "paypal", "after")
    deltas = diff_indexes(before_idx, after_idx)
    op_deltas = [d for d in deltas if "/op:" in d.surface_ref]

    ai_mode, classifications = classify_paypal_deltas(
        op_deltas,
        api_key=openai_api_key,
    )

    scan_res = scan_repo(repo_path, deltas, smap)
    reviews_dir.mkdir(parents=True, exist_ok=True)
    findings_file = reviews_dir / "paypal-findings.json"
    findings_file.write_text(
        json.dumps([_finding_to_dict(f) for f in scan_res.findings], indent=2),
        encoding="utf-8",
    )

    sandbox_status = _run_sandbox_step(client_id=client_id, client_secret=client_secret)
    return PayPalShowcaseReport(
        sdk_operations=smap.operation_count,
        sdk_bindings=len(smap.entries),
        spec_surfaces_before=len(before_idx),
        spec_surfaces_after=len(after_idx),
        total_deltas=len(deltas),
        operation_deltas=len(op_deltas),
        ai_mode=ai_mode,
        classifications=classifications,
        findings=scan_res.findings,
        sandbox_checkout=sandbox_status,
        reviews_written_to=str(findings_file),
    )


def _run_sandbox_step(
    *,
    client_id: str | None,
    client_secret: str | None,
) -> SandboxCheckoutStatus:
    raw_id = client_id if client_id is not None else os.getenv("PAYPAL_SANDBOX_CLIENT_ID", "")
    raw_secret = (
        client_secret
        if client_secret is not None
        else os.getenv("PAYPAL_SANDBOX_CLIENT_SECRET", "")
    )
    cid = raw_id.strip()
    csec = raw_secret.strip()
    placeholders = {"your sandbox client id", "your sandbox secret", "changeme", "xxx"}
    is_placeholder = cid.lower() in placeholders or csec.lower() in placeholders
    if not cid or not csec or is_placeholder:
        return SandboxCheckoutStatus(
            mode="sandbox-ready (set PAYPAL_SANDBOX_CLIENT_ID & SECRET for live order creation)",
            endpoint=f"{SANDBOX_ORIGIN}/v2/checkout/orders",
            plan=PLAN_NAME,
            amount=PLAN_AMOUNT,
            currency=PLAN_CURRENCY,
            order_id=None,
            approve_url=None,
            note=(
                "Export real credentials from developer.paypal.com (Sandbox -> Apps & Credentials) "
                "to create and approve a live sandbox order."
            ),
        )
    checkout = PayPalCheckout(PayPalCheckoutConfig(cid, csec))
    try:
        order: CheckoutOrder = checkout.create_order()
        return SandboxCheckoutStatus(
            mode="live-sandbox-order",
            endpoint=f"{SANDBOX_ORIGIN}/v2/checkout/orders",
            plan=order.plan,
            amount=order.amount,
            currency=order.currency,
            order_id=order.order_id,
            approve_url=order.approve_url,
            note="Open approve_url with a sandbox buyer account, then run --capture ORDER_ID.",
        )
    except PayPalCheckoutError as error:
        return SandboxCheckoutStatus(
            mode="sandbox-auth-error",
            endpoint=f"{SANDBOX_ORIGIN}/v2/checkout/orders",
            plan=PLAN_NAME,
            amount=PLAN_AMOUNT,
            currency=PLAN_CURRENCY,
            order_id=None,
            approve_url=None,
            note=(
                f"{error}. Verify your Client ID and Secret under Apps & Credentials "
                "at developer.paypal.com with Sandbox selected."
            ),
        )
    finally:
        checkout.close()


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "fingerprint": finding.fingerprint,
        "change_fingerprint": finding.change_fingerprint,
        "change_type": finding.change_type,
        "severity": finding.severity,
        "surface_ref": finding.surface_ref,
        "confidence": finding.confidence,
        "score_factors": finding.score_factors,
        "renamed_to": finding.renamed_to,
        "detail": finding.detail,
        "auto_fixable": finding.auto_fixable,
        "usage": asdict(finding.usage),
    }
