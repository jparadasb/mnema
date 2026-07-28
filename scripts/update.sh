#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
echo "Update automation is not implemented safely yet; no changes made." >&2
exit 2

