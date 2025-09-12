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

# Install signal-cli (native) into ./bin if missing or not executable
SIGNAL_BIN="$PROJECT_ROOT/bin/signal-cli"
if [ ! -x "$SIGNAL_BIN" ]; then
  SIGCLI_VER="${SIGCLI_VER:-0.13.18}"
  BASE="https://github.com/AsamK/signal-cli/releases/download/v${SIGCLI_VER}"
  WORKDIR="$PROJECT_ROOT/signal-cli-${SIGCLI_VER}"
  TARBALL="signal-cli-${SIGCLI_VER}-Linux-native.tar.gz"

  rm -rf "$WORKDIR"
  mkdir -p "$WORKDIR"

  # Extract WITHOUT --strip-components (archive may be single-file at root)
  curl -fsSL -o "/tmp/$TARBALL" "${BASE}/${TARBALL}"
  tar -C "$WORKDIR" -xzf "/tmp/$TARBALL"

  # Locate the binary no matter the layout (root or nested dir/bin)
  SRC="$(find "$WORKDIR" -type f -name 'signal-cli' -print -quit || true)"

  if [ -z "$SRC" ]; then
    echo "ERROR: signal-cli not found after extracting $TARBALL"
    echo "Archive contents (first 50 entries):"
    tar -tzf "/tmp/$TARBALL" | head -n 50 || true
    exit 1
  fi

  # Install a *real file* into ./bin (avoid symlinks that can break across deploys)
  install -m 0755 "$SRC" "$SIGNAL_BIN"
fi

# Make sure the app uses exactly this binary
export SIGNAL_CLI_BIN="${SIGNAL_CLI_BIN:-$SIGNAL_BIN}"
export PATH="$PROJECT_ROOT/bin:$PATH"
hash -r

# Debug: show what we’ll run
echo "SIGNAL_CLI_BIN=$SIGNAL_CLI_BIN"
ls -l "$PROJECT_ROOT/bin" || true
"$SIGNAL_CLI_BIN" --version || true

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
