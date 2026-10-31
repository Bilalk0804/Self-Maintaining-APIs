"""Change fixtures shared across tests.

``REAL_RENAME`` is not invented: it was produced by diffing ``stripe/openapi``'s
``spec3.json`` at commit ``30d3391`` against ``bcfd59f``, so the matcher and codemod gates are
exercised against a change Stripe actually shipped.
"""

from __future__ import annotations

from Driftwatch.Surfaces import SurfaceDescriptor
from Driftwatch.Watch.Differ import Delta

REAL_RENAME = Delta(
    surface_ref="api:stripe/op:post_v1_checkout_sessions/param:payment_method_types",
    change_type="param_renamed",
    severity="breaking",
    op="modified",
    before=SurfaceDescriptor(kind="param"),
    after=SurfaceDescriptor(kind="param"),
    renamed_to="api:stripe/op:post_v1_checkout_sessions/param:allowed_payment_method_types",
    detail="payment_method_types -> allowed_payment_method_types",
)

MODEL_DEPRECATED = Delta(
    surface_ref="api:openai/model:gpt-4-vision-preview",
    change_type="model_deprecated",
    severity="deadline",
    op="removed",
    before=SurfaceDescriptor(kind="model"),
    after=None,
    detail="gpt-4-vision-preview shuts down",
)

CHARGE_PARAM_REMOVED = Delta(
    surface_ref="api:stripe/op:post_v1_charges/param:source",
    change_type="param_removed",
    severity="breaking",
    op="removed",
    before=SurfaceDescriptor(kind="param"),
    after=None,
    detail="source removed",
)

CHARGE_FIELD_REMOVED = Delta(
    surface_ref="api:stripe/obj:charge/field:source",
    change_type="response_field_removed",
    severity="breaking",
    op="removed",
    before=SurfaceDescriptor(kind="field"),
    after=None,
    detail="charge.source removed",
)

CHARGE_OP_DEPRECATED = Delta(
    surface_ref="api:stripe/op:post_v1_charges",
    change_type="operation_deprecated",
    severity="deprecating",
    op="modified",
    before=SurfaceDescriptor(kind="op", type_sig="post /v1/charges"),
    after=SurfaceDescriptor(kind="op", type_sig="post /v1/charges", deprecated=True),
    detail="charges deprecated in favour of PaymentIntents",
)

ALL_DELTAS = [REAL_RENAME, MODEL_DEPRECATED, CHARGE_PARAM_REMOVED, CHARGE_FIELD_REMOVED]
