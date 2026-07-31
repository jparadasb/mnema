#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly HARNESS_IMAGE="${MNEMA_STRESS_IMAGE:-mnema:completion-test}"
readonly NETWORK="mnema-power-loss"
readonly MINIO_CONTAINER="mnema-power-loss-minio"
readonly HARNESS_CONTAINER="mnema-power-loss-harness"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || fail "run with sudo"
[[ $# -eq 1 ]] || fail "usage: sudo scripts/recover-power-loss-test.sh WORKSPACE"
workspace="$(realpath -e -- "$1")"
shopt -s nullglob
candidates=("${workspace}"/mnema-power-loss.*)
[[ ${#candidates[@]} -eq 1 ]] || fail "expected exactly one unfinished power-loss drill"
run_root="$(realpath -e -- "${candidates[0]}")"
case "${run_root}" in
  "${workspace}"/mnema-power-loss.*) ;;
  *) fail "power-loss drill escaped configured workspace" ;;
esac
prepared_boot_id="$(tr -d '\n' <"${run_root}/prepared-boot-id")"
current_boot_id="$(tr -d '\n' </proc/sys/kernel/random/boot_id)"
[[ -n "${prepared_boot_id}" && "${prepared_boot_id}" != "${current_boot_id}" ]] ||
  fail "boot ID did not change; physical power-loss test is not proven"

cleanup() {
  docker rm --force "${HARNESS_CONTAINER}" "${MINIO_CONTAINER}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
}

docker rm --force "${HARNESS_CONTAINER}" >/dev/null 2>&1 || true
docker start "${MINIO_CONTAINER}" >/dev/null
for _ in {1..30}; do
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "${MINIO_CONTAINER}")" == "healthy" ]] &&
    break
  sleep 1
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "${MINIO_CONTAINER}")" == "healthy" ]] ||
  fail "isolated MinIO did not recover after power loss"

docker run --rm \
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
  --fault-phase recover \
  --fault-root /drill/fault \
  --backend external \
  --temporary-root /drill/workspace \
  --kopia-password-file /drill/secrets/kopia-password \
  --s3-endpoint "http://${MINIO_CONTAINER}:9000" \
  --s3-bucket mnema-power-loss \
  --s3-access-key-file /drill/secrets/minio-user \
  --s3-secret-key-file /drill/secrets/minio-password \
  --cold-key-file /drill/secrets/cold-key

cleanup
rm -rf -- "${run_root}"
printf 'Physical power-loss recovery verified; disposable drill data removed.\n'
