#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

# Prefer Render's runtime venv; fall back if missing
pick_python() {
  for p in \
    "/opt/render/project/.venv/bin/python" \
    "/opt/render/project/src/.venv/bin/python" \
    "/usr/bin/python3"
  do
    if [ -x "$p" ]; then
      # choose the one that has Flask & Gunicorn
      if "$p" - <<'PY' >/dev/null 2>&1; then
        echo "$p"; return 0
      fi
    fi
  done
  # last resort
  command -v python3
}
# tiny import test
read -r -d '' _PY <<'PY' || true
import importlib.util as iu, sys
need = ["flask", "gunicorn"]
sys.exit(0 if all(iu.find_spec(m) for m in need) else 1)
PY

PYBIN="$(pick_python)"
export PATH="$(dirname "$PYBIN"):$PROJECT_ROOT/bin:$PATH"

# Ensure persistent dirs
mkdir -p /var/foia/media /var/foia/signal-cli
mkdir -p "$PROJECT_ROOT/bin"

# Fetch native signal-cli if missing (fast, no Java)
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
print("f_whisper:", "ok" if ok("faster_whisper") else "missing")
print("torch:",     "ok" if ok("torch")     else "missing")
PY

# Start the app (must bind $PORT)
exec "$PYBIN" -m gunicorn app:app --bind "0.0.0.0:${PORT}"
