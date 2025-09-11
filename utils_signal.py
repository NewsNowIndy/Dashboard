# utils_signal.py
import os
import shutil
import subprocess
from config import Config  # make sure this exists in your project

def _java21_env() -> dict:
    """Return an env that guarantees Java 21 is used."""
    env = os.environ.copy()

    # If current java is already 21, keep it.
    try:
        check = subprocess.run(["java", "-version"], capture_output=True, text=True)
        banner = (check.stderr or check.stdout or "")
        if "version \"21" in banner or "openjdk version \"21" in banner:
            return env
    except Exception:
        pass

    # Try mac helper
    try:
        home = subprocess.check_output(["/usr/libexec/java_home", "-v", "21"], text=True).strip()
        env["JAVA_HOME"] = home
        env["PATH"] = f"{home}/bin:" + env.get("PATH", "")
        return env
    except Exception:
        pass

    # Common Homebrew locations (Apple Silicon / Intel)
    candidates = [
        "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
        "/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home",
    ]
    for p in candidates:
        if os.path.exists(p):
            env["JAVA_HOME"] = p
            env["PATH"] = f"{p}/bin:" + env.get("PATH", "")
            break

    return env

def send_signal_group(group_id: str, text: str) -> None:
    """
    Send a message to a Signal v2 group using signal-cli.
    Raises on ANY failure so callers can log errors clearly.
    """
    # Accept either SIGNAL_NUMBER (preferred) or SIGNAL_ACCOUNT (your shell test)
    number = (
        os.getenv("SIGNAL_NUMBER")
        or os.getenv("SIGNAL_ACCOUNT")
        or getattr(Config, "SIGNAL_NUMBER", None)
    )
    if not number:
        raise RuntimeError("SIGNAL_NUMBER (or SIGNAL_ACCOUNT) is not set")

    signal_bin = os.getenv("SIGNAL_CLI_BIN") or shutil.which("signal-cli") or "signal-cli"
    cmd = [signal_bin, "-u", number, "send", "-g", group_id, "-m", text]

    env = _java21_env()
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)

    # signal-cli prints to stdout/stderr; treat non-zero as failure
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"signal-cli failed (exit {res.returncode}): {err}")
