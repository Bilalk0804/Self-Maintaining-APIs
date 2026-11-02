-- Provider catalog required by source ingestion and indexing migrations.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS api_provider (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            text NOT NULL UNIQUE,
    display_name    text NOT NULL,
    sdk_package     text NOT NULL,
    sdk_repository  text NOT NULL,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_source (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     uuid NOT NULL REFERENCES api_provider(id) ON DELETE CASCADE,
    kind            text NOT NULL,
    url             text NOT NULL,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_ground_truth boolean NOT NULL DEFAULT false,
    poll_interval_s integer NOT NULL DEFAULT 900,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT api_source_kind_ck CHECK (
        kind IN (
            'openapi', 'graphql_sdl', 'sdk_source',
            'changelog_html', 'changelog_rss', 'deprecation_table'
        )
    ),
    CONSTRAINT api_source_url_uq UNIQUE (provider_id, kind, url)
);

CREATE INDEX IF NOT EXISTS api_source_provider_ix
    ON api_source (provider_id, is_active);

INSERT INTO api_provider (slug, display_name, sdk_package, sdk_repository)
VALUES
    ('stripe', 'Stripe', 'stripe', 'https://github.com/stripe/stripe-python'),
    ('paypal', 'PayPal', 'paypal-server-sdk',
        'https://github.com/paypal/PayPal-Python-Server-SDK'),
    ('openai', 'OpenAI', 'openai', 'https://github.com/openai/openai-python'),
    ('razorpay', 'Razorpay', 'razorpay', 'https://github.com/razorpay/razorpay-python')
ON CONFLICT (slug) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    sdk_package = EXCLUDED.sdk_package,
    sdk_repository = EXCLUDED.sdk_repository,
    is_active = true;

INSERT INTO api_source (provider_id, kind, url, is_ground_truth)
SELECT p.id, source.kind, source.url, source.is_ground_truth
FROM api_provider p
JOIN (
    VALUES
        ('stripe', 'openapi', 'https://github.com/stripe/openapi', true),
        ('stripe', 'sdk_source', 'https://github.com/stripe/stripe-python', true),
        ('stripe', 'changelog_html', 'https://docs.stripe.com/changelog', false),
        ('paypal', 'openapi', 'https://github.com/paypal/paypal-rest-api-specifications', true),
        ('paypal', 'sdk_source',
            'https://github.com/paypal/PayPal-Python-Server-SDK', true),
        ('paypal', 'changelog_html', 'https://developer.paypal.com/api/rest/', false),
        ('openai', 'openapi', 'https://github.com/openai/openai-openapi', true),
        ('openai', 'sdk_source', 'https://github.com/openai/openai-python', true),
        ('openai', 'changelog_html',
            'https://platform.openai.com/docs/changelog', false),
        ('razorpay', 'sdk_source', 'https://github.com/razorpay/razorpay-python', true),
        ('razorpay', 'changelog_html', 'https://razorpay.com/docs/api/', false)
) AS source(slug, kind, url, is_ground_truth) ON source.slug = p.slug
ON CONFLICT (provider_id, kind, url) DO UPDATE SET
    is_ground_truth = EXCLUDED.is_ground_truth,
    is_active = true;
