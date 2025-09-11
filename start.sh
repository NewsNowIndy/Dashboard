#!/usr/bin/env bash
set -euxo pipefail

export PROJECT_ROOT="/opt/render/project/src"
export PATH="$PROJECT_ROOT/bin:$PATH"
export SIGNAL_CLI_CONFIG="${SIGNAL_CLI_CONFIG:-/var/foia/signal-cli}"

mkdir -p /var/foia/media /var/foia/signal-cli

# quick sanity checks (won’t fail the boot if signal-cli missing)
which ffmpeg
which signal-cli || true
python - <<'PY'
import torch
import importlib
print("torch:", torch.__version__)
print("whisper:", "ok" if importlib.util.find_spec("whisper") else "missing")
PY

exec gunicorn app:app --bind "0.0.0.0:${PORT}"
