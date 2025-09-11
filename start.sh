#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export PATH="$PROJECT_ROOT/bin:$PATH"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

# Ensure persistent dirs
mkdir -p /var/foia/media /var/foia/signal-cli
mkdir -p "$PROJECT_ROOT/bin"

# --- Verify we have a requirements.txt at repo root (log it so we can see in Render logs)
ls -l "$PROJECT_ROOT/requirements.txt" || true
echo "---- requirements.txt (first 50 lines) ----" || true
head -n 50 "$PROJECT_ROOT/requirements.txt" || true
echo "-------------------------------------------" || true

# --- Install Python deps into the runtime venv (the 'python' here is the runtime one)
python -m pip install --upgrade pip wheel
python -m pip install --no-cache-dir -r "$PROJECT_ROOT/requirements.txt" || true

# Ensure gunicorn + whisper present
python - <<'PY' || true
import pkgutil, sys, subprocess
need = []
for m in ("gunicorn","whisper"):
    if not pkgutil.find_loader(m):
        need.append(m)
if need:
    subprocess.check_call([sys.executable,"-m","pip","install","--no-cache-dir",*need])
PY

# Ensure CPU Torch for openai-whisper
python - <<'PY' || true
import pkgutil, sys, subprocess
if not pkgutil.find_loader("torch"):
    subprocess.check_call([sys.executable,"-m","pip","install","--no-cache-dir","torch==2.4.1","--index-url","https://download.pytorch.org/whl/cpu"])
PY

# --- Fetch native signal-cli if missing (no Java required)
if ! command -v signal-cli >/dev/null 2>&1; then
  SIGCLI_VER=0.13.1
  curl -fsSL -o "$PROJECT_ROOT/bin/signal-cli" "https://github.com/AsamK/signal-cli/releases/download/v${SIGCLI_VER}/signal-cli-native-${SIGCLI_VER}-linux-amd64"
  chmod +x "$PROJECT_ROOT/bin/signal-cli"
fi

# --- Log tool presence
which ffmpeg || true
which signal-cli || true
python - <<'PY' || true
import pkgutil
print("gunicorn:", "ok" if pkgutil.find_loader("gunicorn") else "missing")
print("torch:", "ok" if pkgutil.find_loader("torch") else "missing")
print("whisper:", "ok" if pkgutil.find_loader("whisper") else "missing")
PY

# --- Start app
exec gunicorn app:app --bind "0.0.0.0:${PORT}"
