#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly MINIO_IMAGE="quay.io/minio/minio:RELEASE.2025-07-23T15-54-02Z"
readonly HARNESS_IMAGE="${MNEMA_STRESS_IMAGE:-mnema:completion-test}"
readonly NETWORK="mnema-power-loss"
readonly MINIO_CONTAINER="mnema-power-loss-minio"
readonly HARNESS_CONTAINER="mnema-power-loss-harness"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || fail "run with sudo"
[[ $# -eq 2 && "$2" == "--acknowledge-power-cut" ]] ||
  fail "usage: sudo scripts/prepare-power-loss-test.sh WORKSPACE --acknowledge-power-cut"
workspace="$(realpath -e -- "$1")"
[[ -d "${workspace}" && -w "${workspace}" ]] || fail "workspace must be writable"
shopt -s nullglob
existing_drills=("${workspace}"/mnema-power-loss.*)
((${#existing_drills[@]} == 0)) || fail "an unfinished power-loss drill already exists"
docker container inspect "${MINIO_CONTAINER}" >/dev/null 2>&1 &&
  fail "power-loss MinIO container already exists"
docker network inspect "${NETWORK}" >/dev/null 2>&1 &&
  fail "power-loss network already exists"

run_root="$(mktemp -d "${workspace}/mnema-power-loss.XXXXXX")"
case "${run_root}" in
  "${workspace}"/mnema-power-loss.*) ;;
  *) fail "temporary directory escaped configured workspace" ;;
esac

cleanup() {
  docker rm --force "${HARNESS_CONTAINER}" "${MINIO_CONTAINER}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
  if [[ -d "${run_root}" && "${run_root}" == "${workspace}"/mnema-power-loss.* ]]; then
    rm -rf -- "${run_root}"
  fi
}
trap cleanup EXIT INT TERM

install -d -o 10001 -g 10001 -m 0700 \
  "${run_root}/secrets" \
  "${run_root}/minio-data" \
  "${run_root}/workspace" \
  "${run_root}/workspace/tmp"
chown 10001:10001 "${run_root}"
printf '%s\n' "mnema-power-loss" >"${run_root}/secrets/minio-user"
openssl rand -hex 32 >"${run_root}/secrets/minio-password"
openssl rand 32 >"${run_root}/secrets/cold-key"
openssl rand -base64 48 >"${run_root}/secrets/kopia-password"
cp /proc/sys/kernel/random/boot_id "${run_root}/prepared-boot-id"
chown 10001:10001 "${run_root}/secrets/"*
chmod 0400 "${run_root}/secrets/"*

docker network create --internal "${NETWORK}" >/dev/null
docker run --detach \
  --name "${MINIO_CONTAINER}" \
  --network "${NETWORK}" \
  --user 10001:10001 \
  --env MINIO_ROOT_USER_FILE=/run/secrets/minio-user \
  --env MINIO_ROOT_PASSWORD_FILE=/run/secrets/minio-password \
  --volume "${run_root}/secrets:/run/secrets:ro" \
  --volume "${run_root}/minio-data:/data" \
  --health-cmd "curl --fail --silent http://127.0.0.1:9000/minio/health/live || exit 1" \
  --health-interval 2s \
  --health-timeout 2s \
  --health-retries 30 \
  "${MINIO_IMAGE}" \
  server /data >/dev/null

for _ in {1..30}; do
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "${MINIO_CONTAINER}")" == "healthy" ]] &&
    break
  sleep 1
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "${MINIO_CONTAINER}")" == "healthy" ]] ||
  fail "isolated MinIO did not become healthy"

docker run \
  --name "${HARNESS_CONTAINER}" \
  --network "${NETWORK}" \
  --user 10001:10001 \
  --no-healthcheck \
  --entrypoint python \
  --env TMPDIR=/drill/workspace/tmp \
  --volume "${run_root}:/drill" \
  --volume "$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/stress-test.py"):/stress-test.py:ro" \
  "${HARNESS_IMAGE}" \
  /stress-test.py \
  --mode external-failure \
  --fault-phase attempt \
  --fault-root /drill/fault \
  --fault-bytes 5368709120 \
  --backend external \
  --temporary-root /drill/workspace \
  --kopia-password-file /drill/secrets/kopia-password \
  --s3-endpoint "http://${MINIO_CONTAINER}:9000" \
  --s3-bucket mnema-power-loss \
  --s3-access-key-file /drill/secrets/minio-user \
  --s3-secret-key-file /drill/secrets/minio-password \
  --cold-key-file /drill/secrets/cold-key &
harness_pid=$!

multipart_seen=false
for _ in {1..3600}; do
  kill -0 "${harness_pid}" 2>/dev/null || break
  if [[ -f "${run_root}/fault/upload-started" ]] &&
    find "${run_root}/minio-data/.minio.sys/multipart" -type f -size +0c \
      -print -quit 2>/dev/null | grep -q .; then
    multipart_seen=true
    break
  fi
  sleep 0.1
done
[[ "${multipart_seen}" == "true" ]] || fail "active multipart upload was not observed"

printf '\nPOWER-LOSS TEST READY\n'
printf 'Physically disconnect Raspberry Pi power now. Do not run shutdown or reboot.\n'
printf 'After power returns, run scripts/recover-power-loss-test.sh against: %s\n\n' "${workspace}"

if wait "${harness_pid}"; then
  fail "archive completed because physical power was not disconnected"
fi
fail "archive stopped without a physical power loss; disposable drill data was cleaned"
