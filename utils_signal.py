# utils_signal.py
import os
import shutil
import subprocess

def _signal_bin() -> str:
    explicit = os.getenv("SIGNAL_CLI_BIN")
    if explicit and os.path.exists(explicit) and os.access(explicit, os.X_OK):
        return explicit
    here_bin = os.path.join(os.getenv("PROJECT_ROOT", "/opt/render/project/src"), "bin", "signal-cli")
    if os.path.exists(here_bin) and os.access(here_bin, os.X_OK):
        return here_bin
    found = shutil.which("signal-cli")
    if found:
        return found
    raise FileNotFoundError("signal-cli not found. Install a native binary in ./bin or set SIGNAL_CLI_BIN.")

def _signal_env() -> dict:
    """
    Return an environment for running signal-cli. By default we don't force Java;
    native binaries don't need it. If you must use the Java version, set SIGNAL_CLI_USE_JAVA=1.
    """
    env = os.environ.copy()
    if not os.getenv("SIGNAL_CLI_USE_JAVA"):
        return env  # native or PATH-provided binary
    # (Optional) try to pin Java 21 for Java version only installs
    try:
        check = subprocess.run(["java", "-version"], capture_output=True, text=True)
        banner = (check.stderr or check.stdout or "")
        if "version \"21" in banner or "openjdk version \"21" in banner:
            return env
    except Exception:
        pass
    for p in (
        "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
        "/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home",
    ):
        if os.path.exists(p):
            env["JAVA_HOME"] = p
            env["PATH"] = f"{p}/bin:" + env.get("PATH", "")
            break
    return env

def send_signal_group(group_id: str, text: str) -> None:
    """
    Send a message to a Signal v2 group using signal-cli (native preferred).
    Raises on ANY failure so callers can log errors clearly.
    """
    number = (
        os.getenv("SIGNAL_NUMBER")
        or os.getenv("SIGNAL_ACCOUNT")
    )
    if not number:
        raise RuntimeError("SIGNAL_NUMBER (or SIGNAL_ACCOUNT) is not set")

    cfg = os.getenv("SIGNAL_CLI_CONFIG", "/var/foia/signal-cli")
    os.makedirs(cfg, exist_ok=True)

    cmd = [_signal_bin(), "--config", cfg, "-u", number, "send", "-g", group_id, "-m", text]
    res = subprocess.run(cmd, capture_output=True, text=True, env=_signal_env())

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"signal-cli failed (exit {res.returncode}): {err}")
