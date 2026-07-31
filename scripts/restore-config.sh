#!/usr/bin/env bash
set -Eeuo pipefail
exec /usr/local/bin/mnema restore config "$@"
