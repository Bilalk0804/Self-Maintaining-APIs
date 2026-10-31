"""Provider metadata and source locations used by the watch pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SourceKind = Literal["openapi", "sdk_source", "changelog_html", "changelog_rss"]


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """A provider input that can produce evidence or a surface index."""

    kind: SourceKind
    url: str
    ground_truth: bool


@dataclass(frozen=True, slots=True)
class Provider:
    """Static integration metadata; credentials and polling state stay outside the code."""

    slug: str
    display_name: str
    sdk_package: str
    sdk_repository: str
    sources: tuple[ProviderSource, ...]


PAYPAL = Provider(
    slug="paypal",
    display_name="PayPal",
    sdk_package="paypal-server-sdk",
    sdk_repository="https://github.com/paypal/PayPal-Python-Server-SDK",
    sources=(
        ProviderSource(
            kind="openapi",
            url="https://github.com/paypal/paypal-rest-api-specifications",
            ground_truth=True,
        ),
        ProviderSource(
            kind="sdk_source",
            url="https://github.com/paypal/PayPal-Python-Server-SDK",
            ground_truth=True,
        ),
        ProviderSource(
            kind="changelog_html",
            url="https://developer.paypal.com/api/rest/",
            ground_truth=False,
        ),
    ),
)

STRIPE = Provider(
    slug="stripe",
    display_name="Stripe",
    sdk_package="stripe",
    sdk_repository="https://github.com/stripe/stripe-python",
    sources=(
        ProviderSource(
            kind="openapi",
            url="https://github.com/stripe/openapi",
            ground_truth=True,
        ),
        ProviderSource(
            kind="sdk_source",
            url="https://github.com/stripe/stripe-python",
            ground_truth=True,
        ),
        ProviderSource(
            kind="changelog_html",
            url="https://docs.stripe.com/changelog",
            ground_truth=False,
        ),
    ),
)

OPENAI = Provider(
    slug="openai",
    display_name="OpenAI",
    sdk_package="openai",
    sdk_repository="https://github.com/openai/openai-python",
    sources=(
        ProviderSource(
            kind="openapi",
            url="https://github.com/openai/openai-openapi",
            ground_truth=True,
        ),
        ProviderSource(
            kind="sdk_source",
            url="https://github.com/openai/openai-python",
            ground_truth=True,
        ),
        ProviderSource(
            kind="changelog_html",
            url="https://platform.openai.com/docs/changelog",
            ground_truth=False,
        ),
    ),
)

RAZORPAY = Provider(
    slug="razorpay",
    display_name="Razorpay",
    sdk_package="razorpay",
    sdk_repository="https://github.com/razorpay/razorpay-python",
    sources=(
        ProviderSource(
            kind="sdk_source",
            url="https://github.com/razorpay/razorpay-python",
            ground_truth=True,
        ),
        ProviderSource(
            kind="changelog_html",
            url="https://razorpay.com/docs/api/",
            ground_truth=False,
        ),
    ),
)

PROVIDERS: dict[str, Provider] = {
    provider.slug: provider for provider in (PAYPAL, STRIPE, OPENAI, RAZORPAY)
}


def get_provider(slug: str) -> Provider:
    """Return provider metadata or fail explicitly for an unsupported provider."""
    try:
        return PROVIDERS[slug]
    except KeyError as exc:
        supported = ", ".join(sorted(PROVIDERS))
        message = f"unsupported provider {slug!r}; supported providers: {supported}"
        raise ValueError(message) from exc
