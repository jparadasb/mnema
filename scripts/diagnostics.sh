#!/usr/bin/env bash
set -Eeuo pipefail
echo "Mnema diagnostics (secrets excluded)"
uname -a
docker version --format '{{.Server.Version}}' 2>/dev/null || echo "Docker unavailable"
docker compose -f /opt/mnema/compose.yaml ps 2>/dev/null || true
lsblk --output NAME,TYPE,MODEL,SERIAL,SIZE,FSTYPE,UUID,MOUNTPOINTS
findmnt --json --bytes
curl --fail --silent http://127.0.0.1:8080/healthz || true

