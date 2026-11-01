#!/usr/bin/env bash
# Delegate to the canonical fixture script to prevent security drift.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fetchFixtures.sh" "$@"
