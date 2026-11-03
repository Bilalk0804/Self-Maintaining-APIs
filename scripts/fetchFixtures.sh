#!/usr/bin/env bash
# Fetch the real upstream inputs the spike runs against.
#
# Nothing here is vendored into the repository: the SDK checkout is ~1500 files and each spec
# revision is ~8 MB, and both need to be refreshable to re-run the gates against newer
# upstream state.
set -euo pipefail

CACHE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.cache"
mkdir -p "$CACHE"

# Three revisions of Stripe's GA OpenAPI document. T1 and T2 are four days apart (adjacent
# releases, used to prove the diff is quiet); T0 is a month before T2 (a real release delta).
#
# Only spec3.json is used. The same directory also ships spec3.beta.sdk.json and
# spec3.private_preview.sdk.json; diffing those would raise alerts for unreleased surface.
declare -A SPECS=(
  [spec_T0.json]=30d3391cc09a0f67ad29bee002f570811b19e1da
  [spec_T1.json]=2bab97685e29abae0c241680c2257153e259e50f
  [spec_T2.json]=bcfd59f49dcac84a84d63d304c62793dff6e5916
)

for name in "${!SPECS[@]}"; do
  if [[ -f "$CACHE/$name" ]]; then
    echo "have $name"
    continue
  fi
  sha="${SPECS[$name]}"
  echo "fetching $name (${sha:0:7})"
  curl --proto '=https' --tlsv1.2 -sSfL --max-filesize 25000000 \
    -o "$CACHE/$name.tmp" \
    "https://raw.githubusercontent.com/stripe/openapi/${sha}/openapi/spec3.json"
  mv "$CACHE/$name.tmp" "$CACHE/$name"
done

STRIPE_PYTHON_REF="v11.2.0"

# stripe-python at a pinned tag, the source the Surface Map is derived from.
if [[ -d "$CACHE/stripe-python/stripe" ]]; then
  echo "have stripe-python"
else
  echo "fetching stripe-python ($STRIPE_PYTHON_REF)"
  curl --proto '=https' --tlsv1.2 -sSfL --max-filesize 50000000 \
    -o "$CACHE/sp.tar.gz" \
    "https://codeload.github.com/stripe/stripe-python/tar.gz/refs/tags/${STRIPE_PYTHON_REF}"
  mkdir -p "$CACHE/stripe-python"
  tar xzf "$CACHE/sp.tar.gz" \
    -C "$CACHE/stripe-python" \
    --strip-components=1 \
    --no-same-owner \
    --no-same-permissions
  rm -f "$CACHE/sp.tar.gz"
fi

echo
echo "cache ready:"
du -sh "$CACHE"/* 2>/dev/null || true
