#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
destination="${1:-}"
[[ -n "${destination}" ]] || { echo "usage: $0 /safe/path/mnema-config.tar.gz" >&2; exit 2; }
[[ "${destination}" = /* ]] || { echo "destination must be absolute" >&2; exit 2; }
tar --create --gzip --file "${destination}" \
  --directory / etc/mnema opt/mnema/.env var/lib/mnema/mnema.db
chmod 0600 "${destination}"
echo "Config backup created: ${destination}. It contains secrets; protect it."

