from __future__ import annotations

from pathlib import Path

from Driftwatch.Mapping.SdkPaypal import derive_paypal_python
from Driftwatch.Providers import get_provider


def test_paypal_provider_metadata_points_to_official_sources() -> None:
    provider = get_provider("paypal")

    assert provider.sdk_package == "paypal-server-sdk"
    assert provider.sdk_repository.endswith("PayPal-Python-Server-SDK")
    assert {source.kind for source in provider.sources} == {
        "openapi",
        "sdk_source",
        "changelog_html",
    }


def test_paypal_controller_paths_become_surface_map_entries(tmp_path: Path) -> None:
    controller = tmp_path / "paypalserversdk" / "controllers"
    controller.mkdir(parents=True)
    (controller / "orders_controller.py").write_text(
        """
class OrdersController:
    def create_order(self, options=dict()):
        return (
            RequestBuilder().path("/v2/checkout/orders")
            .http_method(HttpMethodEnum.POST)
            .header_param(Parameter().key("PayPal-Request-Id"))
            .body_param(Parameter().value(options.get("body", None)))
        )
""",
        encoding="utf-8",
    )

    surface_map = derive_paypal_python(tmp_path, sdk_version="2.4.0")

    assert surface_map.operation_count == 1
    assert surface_map.entries[0].surface_ref == "api:paypal/op:post_v2_checkout_orders"
    assert surface_map.entries[0].symbol_ref.endswith(
        "orders_controller.OrdersController.create_order"
    )
    assert surface_map.params_for("api:paypal/op:post_v2_checkout_orders") == frozenset({"body"})
