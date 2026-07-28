#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly INSTALL_ROOT="/opt/mnema"
readonly DATA_ROOT="/var/lib/mnema"
readonly SECRET_ROOT="/etc/mnema/secrets"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

[[ ${EUID} -eq 0 ]] || fail "run as root: sudo ./scripts/install.sh"
architecture="$(dpkg --print-architecture 2>/dev/null || uname -m)"
[[ "${architecture}" == "arm64" || "${architecture}" == "aarch64" ]] ||
  fail "Mnema appliance installer supports ARM64 only; found ${architecture}"
[[ -r /etc/os-release ]] || fail "cannot identify operating system"
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "debian" || "${ID:-}" == "raspbian" ]] ||
  fail "supported systems: Debian or Raspberry Pi OS; found ${ID:-unknown}"

memory_kib="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
[[ "${memory_kib}" -ge 3500000 ]] || fail "at least 4 GB nominal RAM is required"
available_kib="$(df -Pk /opt 2>/dev/null | awk 'NR==2 {print $4}')"
[[ "${available_kib:-0}" -ge 2097152 ]] || fail "at least 2 GiB free under /opt is required"

missing=()
for command in curl jq lsblk findmnt openssl smartctl; do
  command -v "${command}" >/dev/null || missing+=("${command}")
done
if ((${#missing[@]})); then
  info "Installing supported host dependencies: ${missing[*]}"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates curl jq openssl smartmontools util-linux
fi

if ! command -v docker >/dev/null ||
  ! docker compose version >/dev/null 2>&1 ||
  ! docker buildx version >/dev/null 2>&1; then
  info "Installing Docker Engine, Compose, and Buildx from supported Debian packages"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose docker-buildx
fi
systemctl enable --now docker
docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable"

info "Detected block devices (no disk will be formatted):"
lsblk --json --bytes --output NAME,PATH,TYPE,MODEL,SERIAL,SIZE,FSTYPE,UUID,MOUNTPOINTS |
  jq -r '
    .blockdevices[] |
    select(.type == "disk") |
    "model=\(.model // "unknown") serial=\(.serial // "unknown") size=\(.size) path=\(.path)",
    (.children[]? |
      "  filesystem=\(.fstype // "none") uuid=\(.uuid // "none") mounts=\((.mountpoints // []) | join(","))")
  '

[[ -n "${MNEMA_ACTIVE_ROOT:-}" && -n "${MNEMA_BACKUP_ROOT:-}" ]] || fail \
  "set MNEMA_ACTIVE_ROOT and MNEMA_BACKUP_ROOT to existing mounted filesystems"
[[ -d "${MNEMA_ACTIVE_ROOT}" && -d "${MNEMA_BACKUP_ROOT}" ]] || fail \
  "configured active and backup roots must already exist; automatic formatting is disabled"
[[ "${MNEMA_ACTIVE_ROOT}" != *$'\n'* && "${MNEMA_ACTIVE_ROOT}" != *\"* ]] ||
  fail "active storage path contains unsupported characters"
[[ "${MNEMA_BACKUP_ROOT}" != *$'\n'* && "${MNEMA_BACKUP_ROOT}" != *\"* ]] ||
  fail "backup storage path contains unsupported characters"
active_source="$(findmnt -n -o SOURCE --target "${MNEMA_ACTIVE_ROOT}")"
backup_source="$(findmnt -n -o SOURCE --target "${MNEMA_BACKUP_ROOT}")"
active_uuid="$(findmnt -n -o UUID --target "${MNEMA_ACTIVE_ROOT}")"
backup_uuid="$(findmnt -n -o UUID --target "${MNEMA_BACKUP_ROOT}")"
[[ -n "${active_uuid}" && -n "${backup_uuid}" ]] || fail \
  "both storage filesystems must expose UUIDs"
[[ "${active_uuid}" != "${backup_uuid}" ]] || fail \
  "active and backup storage resolve to same filesystem UUID"
[[ "${active_source}" != "${backup_source}" ]] || fail \
  "active and backup storage resolve to same mounted source"
[[ -w "${MNEMA_ACTIVE_ROOT}" && -w "${MNEMA_BACKUP_ROOT}" ]] || fail \
  "both storage roots must be writable"

getent group mnema >/dev/null || groupadd --system --gid 10001 mnema
[[ "$(getent group mnema | cut -d: -f3)" == "10001" ]] ||
  fail "existing mnema group must use GID 10001"
id mnema >/dev/null 2>&1 || useradd --system --uid 10001 --gid mnema \
  --home "${DATA_ROOT}" --shell /usr/sbin/nologin mnema
[[ "$(id -u mnema)" == "10001" ]] || fail "existing mnema user must use UID 10001"
chown mnema:mnema "${MNEMA_ACTIVE_ROOT}" "${MNEMA_BACKUP_ROOT}"
chmod 0750 "${MNEMA_ACTIVE_ROOT}" "${MNEMA_BACKUP_ROOT}"
runuser -u mnema -- test -w "${MNEMA_ACTIVE_ROOT}" ||
  fail "active root must be writable by UID 10001"
runuser -u mnema -- test -w "${MNEMA_BACKUP_ROOT}" ||
  fail "backup root must be writable by UID 10001"
install -d -o root -g root -m 0755 "${INSTALL_ROOT}"
install -d -o mnema -g mnema -m 0750 "${DATA_ROOT}"
install -d -o mnema -g mnema -m 0750 \
  "${DATA_ROOT}/minio" \
  "${DATA_ROOT}/sftpgo-data" \
  "${DATA_ROOT}/sftpgo-home"
install -d -o mnema -g mnema -m 0750 "${MNEMA_ACTIVE_ROOT}/.mnema-staging"
install -d -o mnema -g mnema -m 0750 "${MNEMA_SOURCE_ROOT:-${DATA_ROOT}/test-source}"
install -d -o root -g mnema -m 0750 "${SECRET_ROOT}"

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cp -a \
  "${script_root}/Dockerfile" \
  "${script_root}/LICENSE" \
  "${script_root}/README.md" \
  "${script_root}/compose.yaml" \
  "${script_root}/pyproject.toml" \
  "${script_root}/deploy" \
  "${script_root}/scripts" \
  "${script_root}/src" \
  "${INSTALL_ROOT}/"
ln -sfn "${SECRET_ROOT}" "${INSTALL_ROOT}/secrets"
install -m 0644 "${script_root}/deploy/systemd/mnema.service" /etc/systemd/system/mnema.service
cat >/etc/systemd/system/mnema-smart.service <<EOF
[Unit]
Description=Mnema SMART health collector
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 ${INSTALL_ROOT}/scripts/collect-smart.py --output ${DATA_ROOT}/smart-health.json "${MNEMA_ACTIVE_ROOT}" "${MNEMA_BACKUP_ROOT}"
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${DATA_ROOT}
EOF
cat >/etc/systemd/system/mnema-smart.timer <<'EOF'
[Unit]
Description=Collect Mnema disk SMART health periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
RandomizedDelaySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF
if [[ ! -e "${SECRET_ROOT}/mnema_secret_key" ]]; then
  openssl rand -base64 48 >"${SECRET_ROOT}/mnema_secret_key"
fi
if [[ ! -e "${SECRET_ROOT}/mnema_cold_key" ]]; then
  openssl rand 32 >"${SECRET_ROOT}/mnema_cold_key"
fi
if [[ ! -e "${SECRET_ROOT}/kopia_password" ]]; then
  openssl rand -base64 48 >"${SECRET_ROOT}/kopia_password"
fi
if [[ ! -e "${SECRET_ROOT}/minio_user" ]]; then
  printf '%s\n' "mnema-minio" >"${SECRET_ROOT}/minio_user"
fi
if [[ ! -e "${SECRET_ROOT}/minio_password" ]]; then
  openssl rand -base64 48 >"${SECRET_ROOT}/minio_password"
fi
if [[ ! -e "${SECRET_ROOT}/sftpgo_admin_password" ]]; then
  openssl rand -base64 48 >"${SECRET_ROOT}/sftpgo_admin_password"
fi
if [[ ! -e "${SECRET_ROOT}/sftpgo_user_password" ]]; then
  openssl rand -base64 24 >"${SECRET_ROOT}/sftpgo_user_password"
fi
chmod 0640 "${SECRET_ROOT}/"*
chown root:mnema "${SECRET_ROOT}/"*
onboarding_token="$(openssl rand -base64 32 | tr -d '\n')"
printf '%s\n' "${onboarding_token}" >"${SECRET_ROOT}/onboarding_token"
chmod 0640 "${SECRET_ROOT}/onboarding_token"
chown root:mnema "${SECRET_ROOT}/onboarding_token"

cat >"${INSTALL_ROOT}/.env" <<EOF
MNEMA_DATABASE_URL=sqlite:////var/lib/mnema/mnema.db
MNEMA_ACTIVE_ROOT=/data/active
MNEMA_BACKUP_ROOT=/data/backup
MNEMA_STAGING_ROOT=/data/active/.mnema-staging
MNEMA_SOURCE_ROOT=/data/test-source
MNEMA_HOST_ACTIVE_ROOT=${MNEMA_ACTIVE_ROOT}
MNEMA_HOST_BACKUP_ROOT=${MNEMA_BACKUP_ROOT}
MNEMA_HOST_SOURCE_ROOT=${MNEMA_SOURCE_ROOT:-${DATA_ROOT}/test-source}
MNEMA_HOST_CONFIG_ROOT=${DATA_ROOT}
MNEMA_HOST_MINIO_ROOT=${DATA_ROOT}/minio
MNEMA_HOST_SFTPGO_DATA_ROOT=${DATA_ROOT}/sftpgo-data
MNEMA_HOST_SFTPGO_HOME_ROOT=${DATA_ROOT}/sftpgo-home
MNEMA_SECRET_KEY_FILE=/run/secrets/mnema_secret_key
MNEMA_COLD_ENCRYPTION_KEY_FILE=/run/secrets/mnema_cold_key
MNEMA_KOPIA_PASSWORD_FILE=/run/secrets/kopia_password
MNEMA_KOPIA_REPOSITORY=/data/backup/kopia-repository
MNEMA_KOPIA_CONFIG_FILE=/var/lib/mnema/kopia/repository.config
MNEMA_USE_EXTERNAL_TEST_STORAGE=true
MNEMA_S3_ENDPOINT_URL=http://minio:9000
MNEMA_S3_BUCKET=mnema-integration
MNEMA_S3_ACCESS_KEY_FILE=/run/secrets/minio_user
MNEMA_S3_SECRET_KEY_FILE=/run/secrets/minio_password
MNEMA_SMART_HEALTH_FILE=/var/lib/mnema/smart-health.json
MNEMA_REQUIRE_SMART_HEALTH=true
COMPOSE_PROFILES=integration
MNEMA_GLOBAL_DELETION_ENABLED=false
MNEMA_SAFETY_LOCK=true
EOF
chmod 0600 "${INSTALL_ROOT}/.env"

systemctl daemon-reload
systemctl enable mnema.service
systemctl enable --now mnema-smart.timer
systemctl start mnema-smart.service
docker compose -f "${INSTALL_ROOT}/compose.yaml" config >/dev/null
docker compose -f "${INSTALL_ROOT}/compose.yaml" build
systemctl start mnema.service
for _ in {1..30}; do
  if curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null ||
  fail "stack started but health check failed; run scripts/diagnostics.sh"
for _ in {1..30}; do
  if curl --fail --silent http://127.0.0.1:8081/healthz >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:8081/healthz >/dev/null ||
  fail "SFTPGo started but health check failed; run scripts/diagnostics.sh"
sftpgo_user="${MNEMA_SFTPGO_USER:-${SUDO_USER:-mnema-user}}"
[[ "${sftpgo_user}" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] ||
  fail "SFTPGo username contains unsupported characters"
"${INSTALL_ROOT}/scripts/bootstrap-sftpgo.py" \
  --admin-password-file "${SECRET_ROOT}/sftpgo_admin_password" \
  --api-key-file "${SECRET_ROOT}/sftpgo_api_key" \
  --user "${sftpgo_user}" \
  --user-password-file "${SECRET_ROOT}/sftpgo_user_password"
chmod 0640 "${SECRET_ROOT}/sftpgo_api_key"
chown root:mnema "${SECRET_ROOT}/sftpgo_api_key"

host_address="$(hostname -I | awk '{print $1}')"
info "Mnema installed. Setup URL: http://${host_address:-127.0.0.1}:8080/setup"
info "One-time onboarding token: ${onboarding_token}"
info "SFTP: ${host_address:-127.0.0.1}:2022 user=${sftpgo_user}"
info "SFTP password stored at ${SECRET_ROOT}/sftpgo_user_password"
info "Store token safely. Automatic deletion remains disabled."
