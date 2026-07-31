#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
exec /usr/local/bin/mnema update "$@"
