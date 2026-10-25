#!/usr/bin/env bash
# Run from a reviewed local checkout. No password or token arguments are accepted.
set -euo pipefail
umask 077

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
usage() {
  cat <<'TEXT'
QuantPilot Linux installer (systemd, Python 3.11+)
Usage: sudo bash scripts/install.sh [--port 8080] [--username admin]
       [--origin https://trade.example.com] [--tls-cert /path/origin.crt]
       [--tls-key /path/origin.key] [--upgrade] [--check]
Passwords are entered interactively; secrets must never be command arguments.
Without TLS this binds to 127.0.0.1. Use an SSH tunnel or an HTTPS reverse proxy.
TEXT
}
PORT=""; USERNAME=""; ORIGIN=""; TLS_CERT=""; TLS_KEY=""; UPGRADE=0; CHECK=0
while (($#)); do
  case "$1" in
    --port) PORT="${2:?missing port}"; shift 2;;
    --username) USERNAME="${2:?missing username}"; shift 2;;
    --origin) ORIGIN="${2:?missing origin}"; shift 2;;
    --tls-cert) TLS_CERT="${2:?missing certificate}"; shift 2;;
    --tls-key) TLS_KEY="${2:?missing key}"; shift 2;;
    --upgrade) UPGRADE=1; shift;;
    --check) CHECK=1; shift;;
    --help|-h) usage; exit 0;;
    *) usage >&2; exit 2;;
  esac
done
[[ "$(uname -s)" == Linux ]] || { echo "Linux is required." >&2; exit 1; }
command -v python3 >/dev/null || { echo "Install Python 3.11+ and python3-venv." >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || { echo "Python 3.11+ is required." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required." >&2; exit 1; }
[[ -f "$SOURCE_DIR/requirements.lock" && -f "$SOURCE_DIR/pyproject.toml" ]] || { echo "Run from the complete QuantPilot checkout." >&2; exit 1; }
if ((CHECK)); then echo "Prerequisites OK. No changes made."; exit 0; fi
[[ "$EUID" == 0 ]] || { echo "Run with sudo." >&2; exit 1; }
ROOT_DIR=/opt/quantpilot
DATA_DIR=/var/lib/quantpilot
if [[ -f "$DATA_DIR/config.json" ]] && (( ! UPGRADE )); then
  echo "Existing installation found. Use --upgrade; configuration will be preserved." >&2; exit 1
fi
if ((UPGRADE)); then
  [[ -f "$DATA_DIR/config.json" && -L "$ROOT_DIR/current" ]] || { echo "No existing installation to upgrade." >&2; exit 1; }
else
  if [[ -z "$PORT" ]]; then read -r -p "Web 端口 [8080]: " PORT </dev/tty; PORT="${PORT:-8080}"; fi
  if [[ -z "$USERNAME" ]]; then read -r -p "管理员用户名 [admin]: " USERNAME </dev/tty; USERNAME="${USERNAME:-admin}"; fi
  [[ "$PORT" =~ ^[0-9]{4,5}$ ]] && ((10#$PORT >= 1024 && 10#$PORT <= 65535)) || { echo "Invalid port." >&2; exit 1; }
  [[ "$USERNAME" =~ ^[a-zA-Z0-9_.-]{3,64}$ ]] || { echo "Invalid username." >&2; exit 1; }
  [[ -z "$TLS_CERT" && -z "$TLS_KEY" || -f "$TLS_CERT" && -f "$TLS_KEY" ]] || { echo "Both TLS files are required." >&2; exit 1; }
  if [[ -n "$TLS_CERT" && "$ORIGIN" != https://* ]]; then echo "TLS requires --origin https://your.domain" >&2; exit 1; fi
fi
id quantpilot >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin quantpilot
install -d -m 755 "$ROOT_DIR" "$ROOT_DIR/releases"
install -d -m 700 -o quantpilot -g quantpilot "$DATA_DIR"
RELEASE_DIR="$(mktemp -d "$ROOT_DIR/releases/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
chmod 755 "$RELEASE_DIR"
# Select tracked application files; exclude local credentials, databases and Git history.
for item in quantpilot scripts docs tests pyproject.toml requirements.lock requirements-dev.lock README.md CODEX_TASKS.md; do
  if [[ -e "$SOURCE_DIR/$item" ]]; then cp -R "$SOURCE_DIR/$item" "$RELEASE_DIR/"; fi
done
find "$RELEASE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +
python3 -m venv "$RELEASE_DIR/.venv"
"$RELEASE_DIR/.venv/bin/python" -m pip install --require-hashes -r "$RELEASE_DIR/requirements.lock"
"$RELEASE_DIR/.venv/bin/python" -m pip install --no-deps --no-build-isolation "$RELEASE_DIR"
# Validate configuration before replacing a running release.
if ((UPGRADE)); then
  QP_CONFIG="$DATA_DIR/config.json" "$ROOT_DIR/current/.venv/bin/qp" backup
  QP_CONFIG="$DATA_DIR/config.json" "$RELEASE_DIR/.venv/bin/qp" doctor
else
  BIND_HOST=127.0.0.1
  TLS_ARGS=()
  if [[ -n "$TLS_CERT" ]]; then
    install -d -m 750 -o root -g quantpilot /etc/quantpilot
    install -m 640 -o root -g quantpilot "$TLS_CERT" /etc/quantpilot/origin.crt
    install -m 640 -o root -g quantpilot "$TLS_KEY" /etc/quantpilot/origin.key
    TLS_ARGS=(--tls-cert /etc/quantpilot/origin.crt --tls-key /etc/quantpilot/origin.key)
    BIND_HOST=0.0.0.0
  fi
  ORIGIN="${ORIGIN:-http://127.0.0.1:$PORT}"
  QP_CONFIG="$DATA_DIR/config.json" "$RELEASE_DIR/.venv/bin/qp" init --port "$PORT" --username "$USERNAME" --host "$BIND_HOST" --origin "$ORIGIN" "${TLS_ARGS[@]}" </dev/tty
fi
chown -R quantpilot:quantpilot "$DATA_DIR"
chmod -R go-rwx "$DATA_DIR"
chmod -R a+rX "$RELEASE_DIR"
PREVIOUS="$(readlink "$ROOT_DIR/current" 2>/dev/null || true)"
ln -s "$RELEASE_DIR" "$ROOT_DIR/current.new"
mv -Tf "$ROOT_DIR/current.new" "$ROOT_DIR/current"
install -m 644 "$SOURCE_DIR/scripts/quantpilot.service" /etc/systemd/system/quantpilot.service
cat >/usr/local/bin/qp <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
export QP_CONFIG=/var/lib/quantpilot/config.json
exec /opt/quantpilot/current/.venv/bin/qp "$@"
WRAPPER
chmod 755 /usr/local/bin/qp
ln -sfn /usr/local/bin/qp /usr/local/bin/quantpilot
systemctl daemon-reload
systemctl enable quantpilot
systemctl restart quantpilot
# Certificate verification is not disabled. For an Origin CA certificate,
# readiness is checked via TCP + systemd; external HTTPS is verified at Cloudflare.
READY=0
for attempt in {1..20}; do
  if QP_CONFIG="$DATA_DIR/config.json" "$RELEASE_DIR/.venv/bin/python" -c 'import socket; from quantpilot.config import load_settings; s=load_settings(); socket.create_connection(("127.0.0.1",s.port),2).close()' 2>/dev/null && systemctl is-active --quiet quantpilot; then READY=1; break; fi
  sleep 1
done
if (( ! READY )); then
  echo "New release did not become ready." >&2
  if [[ -n "$PREVIOUS" ]]; then
    ln -s "$PREVIOUS" "$ROOT_DIR/current.rollback"
    mv -Tf "$ROOT_DIR/current.rollback" "$ROOT_DIR/current"
    systemctl restart quantpilot
    echo "Previous release restored; data preserved." >&2
  else
    systemctl stop quantpilot
  fi
  exit 1
fi
echo "QuantPilot installed. Use: sudo qp | sudo qp status | sudo qp logs"
echo "真实交易已锁定。使用设置的 HTTPS 域名，或 SSH 端口转发访问本机端口。"
