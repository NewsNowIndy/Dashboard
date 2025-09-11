#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export PATH="$PROJECT_ROOT/bin:$PATH"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

mkdir -p /var/foia/media /var/foia/signal-cli

# quick sanity checks (non-fatal)
which ffmpeg || true
which signal-cli || true
python - <<'PY' || true
import importlib, sys
def check(m):
    spec = importlib.util.find_spec(m)
    print(f"{m}: {'ok' if spec else 'missing'}")
check("torch")
check("whisper")
PY

exec gunicorn app:app --bind "0.0.0.0:${PORT}"
