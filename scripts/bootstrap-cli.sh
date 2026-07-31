#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

readonly CLI_ROOT="/opt/mnema-cli"
readonly CLI_LINK="/usr/local/bin/mnema"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || fail "run as root: sudo ./scripts/bootstrap-cli.sh"
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ -f "${source_root}/pyproject.toml" ]] || fail "run bootstrap from a Mnema source release"
architecture="$(dpkg --print-architecture 2>/dev/null || uname -m)"
[[ "${architecture}" == "arm64" || "${architecture}" == "aarch64" ]] ||
  fail "Mnema appliance CLI supports ARM64 only; found ${architecture}"

if ! command -v python3 >/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv
fi

install -d -o root -g root -m 0755 "${CLI_ROOT}"
python3 -m venv "${CLI_ROOT}/venv"
"${CLI_ROOT}/venv/bin/python" -m pip install --upgrade "pip==25.1.1"
"${CLI_ROOT}/venv/bin/python" -m pip install "${source_root}"
ln -sfn "${CLI_ROOT}/venv/bin/mnema" "${CLI_LINK}"

printf 'Mnema CLI installed: %s\n' "${CLI_LINK}"
printf 'Next: sudo mnema install --source-root %s\n' "${source_root}"
