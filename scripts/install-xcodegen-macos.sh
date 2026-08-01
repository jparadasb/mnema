#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

readonly XCODEGEN_VERSION="2.38.0"
readonly XCODEGEN_SHA256="aed5bedc80979058287d46b292d3118f89a4cec8e7f1f2ff849e190948c9cd7e"
readonly XCODEGEN_URL="https://github.com/yonaskolb/XcodeGen/releases/download/${XCODEGEN_VERSION}/xcodegen.zip"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./scripts/install-xcodegen-macos.sh [--prefix PATH]

Install Mnema's pinned XcodeGen release without Homebrew or MacPorts.
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
for command in curl shasum unzip; do
  command -v "${command}" >/dev/null || fail "required command not found: ${command}"
done

install_root="${prefix}/share/xcodegen/${XCODEGEN_VERSION}"
binary="${install_root}/xcodegen/bin/xcodegen"
link="${prefix}/bin/xcodegen"

write_launcher() {
  local temporary_launcher="${link}.tmp.$$"
  printf '#!/bin/sh\nexec "%s" "$@"\n' "${binary}" >"${temporary_launcher}"
  chmod 0755 "${temporary_launcher}"
  mv -f "${temporary_launcher}" "${link}"
}

if [[ -x "${binary}" ]] && "${binary}" --version | grep -Fq "${XCODEGEN_VERSION}"; then
  mkdir -p "${prefix}/bin"
  write_launcher
  printf 'XcodeGen %s already installed: %s\n' "${XCODEGEN_VERSION}" "${link}"
  exit 0
fi

temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/mnema-xcodegen.XXXXXX")"
trap 'rm -rf "${temporary_root}"' EXIT
archive="${temporary_root}/xcodegen.zip"
unpacked="${temporary_root}/unpacked"

printf 'Downloading XcodeGen %s...\n' "${XCODEGEN_VERSION}"
curl --fail --location --silent --show-error --output "${archive}" "${XCODEGEN_URL}"
actual_sha256="$(shasum --algorithm 256 "${archive}" | awk '{print $1}')"
[[ "${actual_sha256}" == "${XCODEGEN_SHA256}" ]] || fail "XcodeGen archive checksum mismatch"

mkdir -p "${unpacked}" "${install_root}/xcodegen" "${prefix}/bin"
unzip -q "${archive}" -d "${unpacked}"
[[ -x "${unpacked}/xcodegen/bin/xcodegen" ]] || fail "XcodeGen archive layout is invalid"
cp -R "${unpacked}/xcodegen/." "${install_root}/xcodegen/"
write_launcher
"${link}" --version | grep -Fq "${XCODEGEN_VERSION}" || fail "installed XcodeGen failed validation"

printf 'Installed XcodeGen %s: %s\n' "${XCODEGEN_VERSION}" "${link}"
if [[ ":${PATH}:" != *":${prefix}/bin:"* ]]; then
  printf 'Add this to ~/.zshrc:\n  export PATH="%s/bin:$PATH"\n' "${prefix}"
fi
printf 'Next: cd ios && %s generate\n' "${link}"
