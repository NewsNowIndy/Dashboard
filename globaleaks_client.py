# globaleaks_client.py — login via cURL (auth paths), data via requests
import os, json, base64, random, subprocess, shlex, shutil, requests
from typing import Any, Dict, List, Optional
from argon2.low_level import hash_secret_raw, Type as Argon2Type

BASE = (os.getenv("GLOBALEAKS_BASE_URL", "") or "").rstrip("/")
USER = os.getenv("GLOBALEAKS_USERNAME", "")
PASS = os.getenv("GLOBALEAKS_PASSWORD", "")
AUTHCODE = os.getenv("GLOBALEAKS_AUTHCODE", "")  # usually empty
DEBUG = os.getenv("GLOBALEAKS_DEBUG", "0") not in ("", "0", "false", "False")
CURL_BIN = os.getenv("CURL_BIN") or shutil.which("curl") or "curl"

def _ua():
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36")

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")

class GlobaLeaksClient:
    def __init__(self, base: str, username: str, password: str):
        if not base:
            raise RuntimeError("GLOBALEAKS_BASE_URL is empty")
        self.base = base.rstrip("/")
        self.username = username
        self.password = password

        self.s = requests.Session()
        self.s.trust_env = False
        self.s.headers.update({"User-Agent": _ua()})

        sid_env = (os.getenv("GLOBALEAKS_SESSION_ID", "") or "").strip()
        if sid_env:
            self.s.headers["X-Session"] = sid_env

        if DEBUG:
            print(f"[env] curl at: {CURL_BIN}")

    # ---------- headers for Python GETs ----------
    def _h(self, *, ctype: str) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base,
            "Referer": f"{self.base}/#/login",
            "Content-Type": ctype,
            "User-Agent": _ua(),
        }

    # ---------- cURL helpers ----------
    def _curl_json(self, args: list[str]) -> dict:
        """
        Run curl, capture JSON body + HTTP code.
        IMPORTANT: Do NOT include headers (-i). We only print the body,
        and append HTTPSTATUS via -w so parsing is trivial.
        """
        base_args = [CURL_BIN, "-sS", "--http2", "--fail-with-body",
                    "-w", "HTTPSTATUS:%{http_code}"]
        full = base_args + args
        if DEBUG:
            import shlex
            print("[curl]", " ".join(shlex.quote(a) for a in full))

        p = subprocess.run(full, capture_output=True, text=True)
        out = p.stdout or ""
        err = p.stderr or ""

        # Split body and status
        http_status = None
        if "HTTPSTATUS:" in out:
            body, _, status_tail = out.rpartition("HTTPSTATUS:")
            try:
                http_status = int(status_tail.strip())
            except Exception:
                http_status = None
        else:
            body = out  # fallback; should not happen with -w

        if p.returncode != 0:
            if DEBUG:
                print(f"[curl-err] code={p.returncode} status={http_status} "
                    f"body={body[:300]!r} stderr={err.strip()!r}")
            raise RuntimeError(f"curl failed (status={http_status}, code={p.returncode}): "
                            f"{(body or err).strip()}")

        if DEBUG:
            print(f"[curl-ok] status={http_status} body[0:120]={body[:120]!r}")

        import json
        return json.loads(body or "{}")

    def _curl_token(self) -> str:
        out = self._curl_json([
            "-X", "POST", f"{self.base}/api/auth/token",
            "-H", "Accept: application/json, text/plain, */*",
            "-H", f"Origin: {self.base}",
            "-H", f"User-Agent: { _ua() }",
            "-H", "Content-Type: application/json",
            "--data", "{}",
        ])
        tid = out.get("id") or ""
        if not tid:
            raise RuntimeError("No id from /api/auth/token")
        if DEBUG: print("[auth.token] ->", tid)
        return tid

    def _curl_type_get_salt(self, token_id: str) -> str:
        out = self._curl_json([
            "-X", "POST", f"{self.base}/api/auth/type",
            "-H", "Accept: application/json, text/plain, */*",
            "-H", "X-Requested-With: XMLHttpRequest",
            "-H", f"Referer: {self.base}/#/login",
            "-H", f"Origin: {self.base}",
            "-H", "Content-Type: text/plain; charset=UTF-8",
            "-H", f"X-Token: {token_id}:711",
            "--data", json.dumps({"username": self.username}, separators=(",", ":")),
        ])
        salt = out.get("salt") or ""
        if not salt:
            raise RuntimeError("No 'salt' from /api/auth/type")
        if DEBUG: print("[auth.type] salt len:", len(salt))
        return salt

    def _argon2id_key32(self, salt_b64: str) -> str:
        salt = base64.b64decode(salt_b64)
        k = hash_secret_raw(self.password.encode(), salt,
                            time_cost=2, memory_cost=65536, parallelism=1,
                            hash_len=32, type=Argon2Type.ID)
        key = _b64(k)
        if DEBUG: print("[derive] argon2id key32:", key[:8], "…")
        return key

    def _curl_authentication(self, token_id: str, key32_b64: str) -> Optional[str]:
        out = self._curl_json([
            "-X", "POST", f"{self.base}/api/auth/authentication",
            "-H", "Accept: application/json, text/plain, */*",
            "-H", "X-Requested-With: XMLHttpRequest",
            "-H", f"Referer: {self.base}/#/login",
            "-H", f"Origin: {self.base}",
            "-H", "Content-Type: text/plain; charset=UTF-8",
            "-H", f"X-Token: {token_id}:733",
            "--data", json.dumps({
                "tid": 0,
                "username": self.username,
                "password": key32_b64,
                "authcode": AUTHCODE or ""
            }, separators=(",", ":")),
        ])
        sid = out.get("id")
        if DEBUG: print("[auth.authentication] ->", "OK" if sid else "FAILED")
        return sid

    def _curl_session_login(self, token_id: str, key32_b64: str) -> Optional[str]:
        # backup path: POST /api/auth/session with hashing=argon2id
        out = self._curl_json([
            "-X", "POST", f"{self.base}/api/auth/session",
            "-H", "Accept: application/json, text/plain, */*",
            "-H", "X-Requested-With: XMLHttpRequest",
            "-H", f"Referer: {self.base}/#/login",
            "-H", f"Origin: {self.base}",
            "-H", "Content-Type: application/json",
            "-H", f"X-Token: {token_id}:733",
            "--data", json.dumps({
                "role": "recipient",
                "username": self.username,
                "password": key32_b64,
                "hashing": "argon2id",
                "token": token_id
            }, separators=(",", ":")),
        ])
        sid = out.get("id")
        if DEBUG: print("[auth.session] ->", "OK" if sid else "FAILED")
        return sid

    # ---------- Python data calls ----------
    def _validate_session(self) -> bool:
        r = self.s.get(f"{self.base}/api/recipient/rtips", headers=self._h(ctype="application/json"), timeout=15)
        return r.status_code == 200

    def login(self) -> str:
        # reuse if still valid
        sidh = self.s.headers.get("X-Session")
        if sidh and self._validate_session():
            return sidh
        self.s.headers.pop("X-Session", None)

        # do the same cURL dance you proved works
        t1 = self._curl_token()
        salt_b64 = self._curl_type_get_salt(t1)
        key32 = self._argon2id_key32(salt_b64)
        t2 = self._curl_token()

        sid = self._curl_authentication(t2, key32)
        if not sid:
            # try the session endpoint as a fallback
            sid = self._curl_session_login(t2, key32)
        if not sid:
            raise RuntimeError("Login refused (authentication + session paths).")

        self.s.headers["X-Session"] = sid
        return sid

    def get(self, path: str) -> requests.Response:
        if not self.s.headers.get("X-Session"):
            self.login()
        r = self.s.get(f"{self.base}{path}", headers=self._h(ctype="application/json"), timeout=15)
        if r.status_code == 412:
            self.s.headers.pop("X-Session", None)
            self.login()
            r = self.s.get(f"{self.base}{path}", headers=self._h(ctype="application/json"), timeout=15)
        r.raise_for_status()
        return r

    def list_tips(self) -> List[Dict[str, Any]]:
        return self.get("/api/recipient/rtips").json() or []

# convenience
def list_tips() -> List[Dict[str, Any]]:
    return GlobaLeaksClient(BASE, USER, PASS).list_tips()
