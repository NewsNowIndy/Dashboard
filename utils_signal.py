# utils_signal.py
import os
import subprocess

SIGNAL_ACCOUNT = os.getenv("SIGNAL_ACCOUNT", "").strip()  # e.g. +1317XXXXXXX

def send_signal_group(group_id: str, text: str, attachments=None) -> bool:
    """
    Send a message to a Signal V2 group using signal-cli (linked device).
    Requires: brew install signal-cli
    Link once: signal-cli -u +1YOURNUMBER link -n "foia-bot" (scan QR in Signal)
    """
    if not SIGNAL_ACCOUNT:
        print("utils_signal: Set SIGNAL_ACCOUNT env var (e.g. +1317xxxxxxx).")
        return False
    if not group_id:
        print("utils_signal: group_id missing.")
        return False
    if not text:
        return True

    cmd = ["signal-cli", "-u", SIGNAL_ACCOUNT, "send", "-g", group_id, "-m", text]
    for a in (attachments or []):
        cmd += ["-a", a]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("signal-cli failed:", (proc.stderr or proc.stdout).strip())
        return False
    return True
