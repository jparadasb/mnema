#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly MINIO_IMAGE="quay.io/minio/minio:RELEASE.2025-07-23T15-54-02Z"
readonly HARNESS_IMAGE="${MNEMA_STRESS_IMAGE:-mnema:0.1.0}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || fail "run with sudo so Docker and isolated workspace cleanup are reliable"
[[ $# -ge 1 ]] || fail "usage: sudo scripts/run-external-stress.sh WORKSPACE [stress options]"

workspace="$(realpath -e -- "$1")"
shift
[[ -d "${workspace}" && -w "${workspace}" ]] || fail "workspace must be an existing writable directory"

minio_restart_test=false
if [[ ${1:-} == "--minio-restart-test" ]]; then
  minio_restart_test=true
  shift
fi

run_root="$(mktemp -d "${workspace}/mnema-external-stress.XXXXXX")"
case "${run_root}" in
  "${workspace}"/mnema-external-stress.*) ;;
  *) fail "temporary directory escaped configured workspace" ;;
esac

network="mnema-stress-${$}"
minio_container="mnema-stress-minio-${$}"

cleanup() {
  docker rm --force "${minio_container}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  if [[ -d "${run_root}" && "${run_root}" == "${workspace}"/mnema-external-stress.* ]]; then
    rm -rf -- "${run_root}"
  fi
}
trap cleanup EXIT INT TERM

install -d -o 10001 -g 10001 -m 0700 \
  "${run_root}/secrets" \
  "${run_root}/minio-data" \
  "${run_root}/workspace"
chown 10001:10001 "${run_root}"
chmod 0700 "${run_root}"
printf '%s\n' "mnema-stress" >"${run_root}/secrets/minio-user"
openssl rand -hex 32 >"${run_root}/secrets/minio-password"
openssl rand 32 >"${run_root}/secrets/cold-key"
openssl rand -base64 48 >"${run_root}/secrets/kopia-password"
chown 10001:10001 "${run_root}/secrets/"*
chmod 0400 "${run_root}/secrets/"*

docker network create --internal "${network}" >/dev/null
docker run --detach \
  --name "${minio_container}" \
  --network "${network}" \
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
  status="$(docker inspect --format '{{.State.Health.Status}}' "${minio_container}")"
  [[ "${status}" == "healthy" ]] && break
  [[ "${status}" == "unhealthy" ]] && fail "isolated MinIO became unhealthy"
  sleep 1
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "${minio_container}")" == "healthy" ]] ||
  fail "isolated MinIO did not become healthy"

run_harness() {
  docker run --rm \
    --network "${network}" \
    --user 10001:10001 \
    --entrypoint python \
    --volume "${run_root}:/stress" \
    --volume "$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/stress-test.py"):/stress-test.py:ro" \
    "${HARNESS_IMAGE}" \
    /stress-test.py \
    "$@" \
    --backend external \
    --temporary-root /stress/workspace \
    --kopia-password-file /stress/secrets/kopia-password \
    --s3-endpoint "http://${minio_container}:9000" \
    --s3-bucket mnema-stress \
    --s3-access-key-file /stress/secrets/minio-user \
    --s3-secret-key-file /stress/secrets/minio-password \
    --cold-key-file /stress/secrets/cold-key
}

if [[ "${minio_restart_test}" == "false" ]]; then
  run_harness "$@"
  exit
fi

attempt_output="${run_root}/attempt.json"
run_harness \
  --mode external-failure \
  --fault-phase attempt \
  --fault-root /stress/fault \
  "$@" >"${attempt_output}" &
attempt_pid=$!

multipart_seen=false
for _ in {1..1200}; do
  if ! kill -0 "${attempt_pid}" 2>/dev/null; then
    break
  fi
  if find "${run_root}/minio-data/.minio.sys/multipart" -type f -size +0c \
    -print -quit 2>/dev/null | grep -q .; then
    multipart_seen=true
    break
  fi
  sleep 0.1
done
[[ "${multipart_seen}" == "true" ]] || fail "no active multipart upload observed"

docker stop --time 1 "${minio_container}" >/dev/null
if ! wait "${attempt_pid}"; then
  fail "fault attempt process failed before recording expected interruption"
fi
docker start "${minio_container}" >/dev/null

for _ in {1..30}; do
  status="$(docker inspect --format '{{.State.Health.Status}}' "${minio_container}")"
  [[ "${status}" == "healthy" ]] && break
  sleep 1
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "${minio_container}")" == "healthy" ]] ||
  fail "isolated MinIO did not recover"

cat "${attempt_output}"
run_harness \
  --mode external-failure \
  --fault-phase recover \
  --fault-root /stress/fault \
  "$@"
