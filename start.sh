#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export PATH="$PROJECT_ROOT/bin:$PATH"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

# Ensure persistent dirs
mkdir -p /var/foia/media /var/foia/signal-cli
mkdir -p "$PROJECT_ROOT/bin"

# If signal-cli isn’t present, fetch native binary (no Java needed)
if ! command -v signal-cli >/dev/null 2>&1; then
  SIGCLI_VER=0.13.1
  curl -fsSL -o "$PROJECT_ROOT/bin/signal-cli" "https://github.com/AsamK/signal-cli/releases/download/v${SIGCLI_VER}/signal-cli-native-${SIGCLI_VER}-linux-amd64"
  chmod +x "$PROJECT_ROOT/bin/signal-cli"
fi

# Quick checks (non-fatal)
which ffmpeg || true
which signal-cli || true
python - <<'PY' || true
import importlib.util as iu
def ok(m): return iu.find_spec(m) is not None
print("gunicorn:", "ok" if ok("gunicorn") else "missing")
print("whisper:",  "ok" if ok("whisper")  else "missing")
print("torch:",    "ok" if ok("torch")    else "missing")
PY

# Start your app (must bind 0.0.0.0:$PORT)
exec gunicorn app:app --bind "0.0.0.0:${PORT}"
