#!/bin/zsh
# =========================================
# FOIA Dashboard - Signal-only alert runner
# Schedules well with cron or launchd.
# =========================================

# ---- logging (append) ----
exec >>"$HOME/Library/Logs/foiaalerts.cron.log" 2>&1
echo "=== $(date) run ==="

set -euo pipefail

# ---- project dir ----
PROJECT_DIR="$HOME/Desktop/foia_dashboard"

# ---- Force Java 21 for signal-cli (cron has a minimal env) ----
if [ -d /opt/homebrew/opt/openjdk@21 ]; then
  export JAVA_HOME="/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home"   # Apple Silicon (brew)
elif [ -d /usr/local/opt/openjdk@21 ]; then
  export JAVA_HOME="/usr/local/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home"      # Intel (brew)
else
  echo "[WARN] openjdk@21 not found in Homebrew paths. Install it: brew install openjdk@21"
  export JAVA_HOME="${JAVA_HOME:-}"
fi

# Put JDK 21 first; include brew bins so signal-cli is discoverable
export PATH="$JAVA_HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# ---- debug: show what cron actually sees ----
echo "JAVA_HOME=$JAVA_HOME"
command -v java || true
java -version || true
command -v signal-cli || true
signal-cli --version || true

# ---- app env (allow overrides from shell/launchd; set defaults if empty) ----
export SIGNAL_ACCOUNT="${SIGNAL_ACCOUNT:-+1317XXXXXXX}"   # <-- your Signal phone number in E.164
export SIGNAL_GROUP_ID="${SIGNAL_GROUP_ID:-PASTE_GROUP_ID}"  # <-- your Signal V2 group Id
export APP_BASE_URL="${APP_BASE_URL:-http://localhost:5000}"

# Validate critical envs
if [ -z "${SIGNAL_ACCOUNT}" ] || [ -z "${SIGNAL_GROUP_ID}" ]; then
  echo "[ERROR] SIGNAL_ACCOUNT and/or SIGNAL_GROUP_ID are not set."
  exit 1
fi

# ---- activate venv ----
if [ ! -f "$PROJECT_DIR/.venv/bin/activate" ]; then
  echo "[ERROR] Python venv not found at $PROJECT_DIR/.venv. Create it and install deps."
  exit 1
fi
source "$PROJECT_DIR/.venv/bin/activate"

# ---- run the task (no --force: respects 21/14/7 & weekly cadence) ----
cd "$PROJECT_DIR"
"$PROJECT_DIR/.venv/bin/python" -m flask --app app send-alerts --force

# For an on-demand test run, temporarily add --force:
# "$PROJECT_DIR/.venv/bin/python" -m flask --app app send-alerts --force

echo "[OK] send-alerts finished"

