#!/usr/bin/env bash
set -Eeuo pipefail
destination="${1:-}"
[[ -n "${destination}" ]] || { echo "usage: $0 /safe/path/mnema-config.tar.gz" >&2; exit 2; }
exec /usr/local/bin/mnema backup create "${destination}"
