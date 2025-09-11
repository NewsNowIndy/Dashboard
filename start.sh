#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

# Prefer a real venv; try both common Render locations, then fall back to system python3
pick_python() {
  for v in "/opt/render/project/.venv/bin/python" "/opt/render/project/src/.venv/bin/python" "/usr/bin/python3"; do
    if [ -x "$v" ]; then
      echo "$v"
      return 0
    fi
  done
  # last resort
  command -v python
}

PYBIN="$(pick_python)"
export PATH="$(dirname "$PYBIN"):$PROJECT_ROOT/bin:$PATH"

# Ensure persistent dirs
mkdir -p /var/foia/media /var/foia/signal-cli
mkdir -p "$PROJECT_ROOT/bin"

# If signal-cli isn’t present, fetch Linux-native tarball and extract it
if ! command -v signal-cli >/dev/null 2>&1; then
  SIGCLI_VER="${SIGCLI_VER:-0.13.18}"
  TARBALL="signal-cli-${SIGCLI_VER}-Linux-native.tar.gz"
  BASE_URL="https://github.com/AsamK/signal-cli/releases/download/v${SIGCLI_VER}"
  tmp="/tmp/${TARBALL}"
  dest_dir="$PROJECT_ROOT/signal-cli-${SIGCLI_VER}"
  rm -rf "$dest_dir"
  mkdir -p "$dest_dir" "$PROJECT_ROOT/bin"
  curl -fsSL -o "$tmp" "${BASE_URL}/${TARBALL}"
  tar -C "$dest_dir" --strip-components=1 -xzf "$tmp"
  ln -sf "$dest_dir/bin/signal-cli" "$PROJECT_ROOT/bin/signal-cli"
fi

# --- Ensure gunicorn is available in the chosen interpreter (quick install if missing) ---
"$PYBIN" - <<'PY' || true
import importlib.util as iu, sys, subprocess
if iu.find_spec("gunicorn") is None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "gunicorn>=22.0"])
PY

# Quick visibility (non-fatal)
which python || true
python -c 'import sys; print("python:", sys.executable)' || true
which ffmpeg || true
which signal-cli || true
python - <<'PY' || true
import importlib.util as iu
chk=lambda m: "ok" if iu.find_spec(m) else "missing"
print("gunicorn:", chk("gunicorn"))
print("whisper:",  chk("whisper"))
print("torch:",    chk("torch"))
PY

# Bind to the provided $PORT so Render's port scan passes
exec "$PYBIN" -m gunicorn app:app --bind "0.0.0.0:${PORT}"
