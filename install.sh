#!/usr/bin/env bash
# Public bootstrap. The caller must pin a reviewed full commit ID.
set -euo pipefail
umask 077

REF=""
INSTALL_ARGS=()
while (($#)); do
  case "$1" in
    --ref) REF="${2:?missing full commit ID}"; shift 2;;
    --help|-h)
      cat <<'HELP'
QuantPilot public bootstrap
Usage: bash install.sh --ref <40-character-commit> [installer options]
Options forwarded to the installer include --port, --username, --origin,
--tls-cert, --tls-key, --upgrade and --check.
Passwords are entered interactively. No GitHub login or token is needed.
Requires Linux/systemd, Python 3.11+, venv, curl and tar.
HELP
      exit 0;;
    *) INSTALL_ARGS+=("$1"); shift;;
  esac
done
[[ "$REF" =~ ^[0-9a-f]{40}$ ]] || { echo "A full 40-character commit ID is required (--ref)." >&2; exit 2; }
command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }
command -v tar >/dev/null || { echo "tar is required." >&2; exit 1; }
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
curl --proto '=https' --tlsv1.2 --fail --show-error --silent --location \
  --retry 2 --max-time 120 \
  "https://codeload.github.com/Jk-z-Box/QuantPilot/tar.gz/$REF" \
  --output "$TEMP_DIR/source.tar.gz"
mkdir "$TEMP_DIR/source"
tar -xzf "$TEMP_DIR/source.tar.gz" --strip-components=1 -C "$TEMP_DIR/source"
[[ -f "$TEMP_DIR/source/scripts/install.sh" && -f "$TEMP_DIR/source/requirements.lock" ]] || { echo "Incomplete source archive." >&2; exit 1; }
if [[ "$EUID" == 0 ]]; then
  bash "$TEMP_DIR/source/scripts/install.sh" "${INSTALL_ARGS[@]}"
else
  sudo bash "$TEMP_DIR/source/scripts/install.sh" "${INSTALL_ARGS[@]}"
fi
