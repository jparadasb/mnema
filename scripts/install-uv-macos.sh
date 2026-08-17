#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

readonly UV_VERSION="0.12.5"
readonly UV_SHA256_ARM64="5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62"
readonly UV_SHA256_X86_64="b3b2137477cf96c9686ebfb71524614cec780c673fd73e59bce099aef02e70e8"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./scripts/install-uv-macos.sh [--prefix PATH]

Install Mnema's pinned uv release without Homebrew or MacPorts. uv provides
the Python toolchain for the iOS runner, which cannot use actions/setup-python
because that action installs Python with sudo.
Default prefix: $HOME/.local
EOF
}

prefix="${HOME:?HOME is required}/.local"
while (($#)); do
  case "$1" in
    --prefix)
      (($# >= 2)) || fail "--prefix requires a path"
      prefix="$2"
      shift 2
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || fail "this installer supports macOS only"
[[ "${prefix}" == /* ]] || fail "--prefix must be an absolute path"
[[ "${prefix}" != *[$'\n\r"\\']* ]] || fail "--prefix contains unsupported characters"
for command in curl shasum tar; do
  command -v "${command}" >/dev/null || fail "required command not found: ${command}"
done

case "$(uname -m)" in
  arm64 | aarch64)
    readonly UV_TARGET="aarch64-apple-darwin"
    readonly UV_SHA256="${UV_SHA256_ARM64}"
    ;;
  x86_64)
    readonly UV_TARGET="x86_64-apple-darwin"
    readonly UV_SHA256="${UV_SHA256_X86_64}"
    ;;
  *)
    fail "unsupported architecture: $(uname -m)"
    ;;
esac

readonly UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_TARGET}.tar.gz"

install_root="${prefix}/share/uv/${UV_VERSION}"
binary="${install_root}/uv-${UV_TARGET}/uv"
link="${prefix}/bin/uv"

write_launcher() {
  local temporary_launcher="${link}.tmp.$$"
  printf '#!/bin/sh\nexec "%s" "$@"\n' "${binary}" >"${temporary_launcher}"
  chmod 0755 "${temporary_launcher}"
  mv -f "${temporary_launcher}" "${link}"
}

if [[ -x "${binary}" ]] && "${binary}" --version | grep -Fq "${UV_VERSION}"; then
  mkdir -p "${prefix}/bin"
  write_launcher
  printf 'uv %s already installed: %s\n' "${UV_VERSION}" "${link}"
  exit 0
fi

temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/mnema-uv.XXXXXX")"
trap 'rm -rf "${temporary_root}"' EXIT
archive="${temporary_root}/uv.tar.gz"
unpacked="${temporary_root}/unpacked"

printf 'Downloading uv %s (%s)...\n' "${UV_VERSION}" "${UV_TARGET}"
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  --output "${archive}" "${UV_URL}"
actual_sha256="$(shasum --algorithm 256 "${archive}" | awk '{print $1}')"
[[ "${actual_sha256}" == "${UV_SHA256}" ]] || fail "uv archive checksum mismatch"

mkdir -p "${unpacked}" "${install_root}" "${prefix}/bin"
tar -xzf "${archive}" -C "${unpacked}"
[[ -x "${unpacked}/uv-${UV_TARGET}/uv" ]] || fail "uv archive layout is invalid"
cp -R "${unpacked}/uv-${UV_TARGET}" "${install_root}/"
write_launcher
"${link}" --version | grep -Fq "${UV_VERSION}" || fail "installed uv failed validation"

printf 'Installed uv %s: %s\n' "${UV_VERSION}" "${link}"
if [[ ":${PATH}:" != *":${prefix}/bin:"* ]]; then
  printf 'Add this to ~/.zshrc:\n  export PATH="%s/bin:$PATH"\n' "${prefix}"
fi
