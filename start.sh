#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

# Pick a python; prefer the app venv
pick_python() {
  for p in "/opt/render/project/src/.venv/bin/python" "/opt/render/project/.venv/bin/python" "/usr/bin/python3"; do
    [ -x "$p" ] && echo "$p" && return 0
  done
  command -v python3
}

PYBIN="$(pick_python)"
export PATH="$(dirname "$PYBIN"):$PROJECT_ROOT/bin:$PATH"

# Ensure dirs
mkdir -p /var/foia/media /var/foia/signal-cli "$PROJECT_ROOT/bin"

# Fetch native signal-cli (no Java) if missing
if ! command -v signal-cli >/dev/null 2>&1; then
  SIGCLI_VER="${SIGCLI_VER:-0.13.18}"
  TARBALL="signal-cli-${SIGCLI_VER}-Linux-native.tar.gz"
  BASE_URL="https://github.com/AsamK/signal-cli/releases/download/v${SIGCLI_VER}"
  TMP="/tmp/${TARBALL}"
  DEST="$PROJECT_ROOT/signal-cli-${SIGCLI_VER}"
  rm -rf "$DEST"
  mkdir -p "$DEST" "$PROJECT_ROOT/bin"
  curl -fsSL -o "$TMP" "${BASE_URL}/${TARBALL}"
  tar -C "$DEST" --strip-components=1 -xzf "$TMP"
  ln -sf "$DEST/bin/signal-cli" "$PROJECT_ROOT/bin/signal-cli"
fi

# Quick visibility (non-fatal)
which python || true
python -c 'import sys; print("python:", sys.executable)' || true
which ffmpeg || true
which signal-cli || true
python - <<'PY' || true
import importlib.util as iu
def ok(m): return iu.find_spec(m) is not None
print("flask:",     "ok" if ok("flask")     else "missing")
print("gunicorn:",  "ok" if ok("gunicorn")  else "missing")
print("whisper:",   "ok" if ok("whisper")   else "missing")
print("torch:",     "ok" if ok("torch")     else "missing")
PY

# Bind to $PORT for Render
exec "$PYBIN" -m gunicorn app:app --bind "0.0.0.0:${PORT}"
