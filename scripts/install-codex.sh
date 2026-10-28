#!/usr/bin/env bash
# Official pinned binary; no npm lifecycle scripts, credentials or dynamic "latest".
set -euo pipefail
umask 077
[[ "$EUID" == 0 ]] || { echo "Run as root." >&2; exit 1; }
case "$(uname -m)" in
  x86_64) ARCH=x86_64; DIGEST=be635d3486a5bd060d0fff582ae6d30160280c31344dd85719707fa920e187f0; BWRAP_DIGEST=6524527367e2c8e7fcf07a08775cddac6d6cfa8f0bec4bf60aae510bc97532fe;;
  aarch64|arm64) ARCH=aarch64; DIGEST=6522e056d0be7f531da915c9a81ae05bc70811b782542499cc0a9776d3abf5d3; BWRAP_DIGEST=ffde98edd6345ee09486d5c0afca041bc21f18926d09c730a3089de56cdbbf19;;
  *) echo "Codex bridge requires Linux x86_64 or aarch64." >&2; exit 1;;
esac
VERSION=0.165.0
DEST="/opt/quantpilot/codex/$VERSION"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
if [[ ! -x "$DEST/codex" ]]; then
  curl --proto '=https' --tlsv1.2 --fail --show-error --silent --location --retry 2 --max-time 300 \
    "https://github.com/openai/codex/releases/download/rust-v$VERSION/codex-$ARCH-unknown-linux-musl.tar.gz" -o "$TMP/codex.tar.gz"
  printf '%s  %s\n' "$DIGEST" "$TMP/codex.tar.gz" | sha256sum --check --status
  tar -xzf "$TMP/codex.tar.gz" -C "$TMP"
  install -d -m 755 "$DEST"
  install -m 755 "$TMP/codex-$ARCH-unknown-linux-musl" "$DEST/codex"
fi
if [[ ! -x "$DEST/codex-resources/bwrap" ]]; then
  curl --proto '=https' --tlsv1.2 --fail --show-error --silent --location --retry 2 --max-time 120 \
    "https://github.com/openai/codex/releases/download/rust-v$VERSION/bwrap-$ARCH-unknown-linux-musl.tar.gz" -o "$TMP/bwrap.tar.gz"
  printf '%s  %s\n' "$BWRAP_DIGEST" "$TMP/bwrap.tar.gz" | sha256sum --check --status
  tar -xzf "$TMP/bwrap.tar.gz" -C "$TMP"
  install -d -m 755 "$DEST/codex-resources"
  install -m 755 "$TMP/bwrap-$ARCH-unknown-linux-musl" "$DEST/codex-resources/bwrap"
fi
"$DEST/codex" --version | python3 -c 'import sys; assert sys.stdin.read().strip() == "codex-cli 0.165.0"'
id quantpilot-ai >/dev/null 2>&1 || useradd --system --home-dir /var/lib/quantpilot-ai --shell /usr/sbin/nologin quantpilot-ai
install -d -m 700 -o quantpilot-ai -g quantpilot /var/lib/quantpilot-ai
install -d -m 700 -o quantpilot-ai -g quantpilot /var/lib/quantpilot-ai/empty
# Preserve official auth storage during every upgrade. This root-only backup is separate from trading backups.
if [[ -f /var/lib/quantpilot-ai/auth.json ]]; then
  install -d -m 700 /var/backups/quantpilot-ai
  tar -czf "/var/backups/quantpilot-ai/$(date -u +%Y%m%dT%H%M%SZ).tar.gz" -C /var/lib/quantpilot-ai auth.json
fi
