#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

# Prefer the venv that actually has gunicorn
VENV_CANDIDATES=(
  "/opt/render/project/.venv"
  "/opt/render/project/src/.venv"
)

choose_python() {
  for v in "${VENV_CANDIDATES[@]}"; do
    if [ -x "$v/bin/python" ]; then
      if "$v/bin/python" - <<'PY' >/dev/null 2>&1; then
        echo "$v/bin/python"
        return 0
      fi
    fi
  done
  # fallback to whatever python is on PATH (shouldn’t happen)
  command -v python
}
# Tiny Python that exits 0 only if gunicorn is importable
read -r -d '' PY <<'PY'
import importlib.util as iu, sys
sys.exit(0 if iu.find_spec("gunicorn") else 1)
PY

PYBIN="$(choose_python)"
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

# Quick visibility
which python || true
python -c 'import sys; print("python:", sys.executable)' || true
which ffmpeg || true
which signal-cli || true
python - <<'PY' || true
import importlib.util as iu
def ok(m): return iu.find_spec(m) is not None
print("gunicorn:", "ok" if ok("gunicorn") else "missing")
print("whisper:",  "ok" if ok("whisper")  else "missing")
print("torch:",    "ok" if ok("torch")    else "missing")
PY

# Launch the app from the venv that has gunicorn
exec "$PYBIN" -m gunicorn app:app --bind "0.0.0.0:${PORT}"
