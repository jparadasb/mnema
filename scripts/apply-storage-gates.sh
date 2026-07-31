#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || fail "run with sudo"
[[ $# -eq 2 ]] || fail "usage: sudo scripts/apply-storage-gates.sh ACTIVE_ROOT BACKUP_ROOT"
active_root="$(realpath -e -- "$1")"
backup_root="$(realpath -e -- "$2")"
for path in "${active_root}" "${backup_root}"; do
  [[ "${path}" == /* && "${path}" != *[[:space:]\\]* ]] ||
    fail "storage paths must be absolute and cannot contain whitespace or backslashes"
  mountpoint --quiet "${path}" || fail "storage path is not a mountpoint: ${path}"
done
active_uuid="$(findmnt -n -o UUID --target "${active_root}")"
backup_uuid="$(findmnt -n -o UUID --target "${backup_root}")"
[[ -n "${active_uuid}" && -n "${backup_uuid}" && "${active_uuid}" != "${backup_uuid}" ]] ||
  fail "storage mount UUIDs must exist and differ"

install -d -o root -g root -m 0755 /etc/systemd/system/docker.service.d
cat >/etc/systemd/system/docker.service.d/mnema-storage.conf <<EOF
[Unit]
RequiresMountsFor=${active_root} ${backup_root}

[Service]
ExecStartPre=/usr/bin/mountpoint --quiet ${active_root}
ExecStartPre=/usr/bin/mountpoint --quiet ${backup_root}
EOF
install -d -o root -g root -m 0755 /etc/systemd/system/mnema.service.d
cat >/etc/systemd/system/mnema.service.d/storage.conf <<EOF
[Unit]
RequiresMountsFor=${active_root} ${backup_root}

[Service]
ExecStartPre=/usr/bin/mountpoint --quiet ${active_root}
ExecStartPre=/usr/bin/mountpoint --quiet ${backup_root}
EOF
systemctl daemon-reload
systemd-analyze verify docker.service mnema.service >/dev/null
printf 'Storage gates installed; they apply on next Docker or Mnema service start.\n'
