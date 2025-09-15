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

    # ---------- exact header set used by the browser ----------
    def _h_common(self) -> list[str]:
        base = self.base
        return [
            "-H", "Accept: application/json, text/plain, */*",
            "-H", f"Origin: {base}",
            "-H", f"Referer: {base}/#/login",
            "-H", f"User-Agent: {_ua()}",
        ]

    # ---------- cURL helpers ----------
    def _curl_json(self, args: list[str], *, http2: bool = False) -> dict:
        """
        Run curl (h1 by default; h2 if requested), capture JSON body + HTTP status.
        """
        base_args = [
            CURL_BIN, "-sS", "--fail-with-body",
            "-w", "HTTPSTATUS:%{http_code}",
        ]
        # choose protocol explicitly to avoid proxy quirks
        base_args.insert(2, "--http2" if http2 else "--http1.1")

        full = base_args + args
        if DEBUG:
            import shlex
            print("[curl]", " ".join(shlex.quote(a) for a in full))

        p = subprocess.run(full, capture_output=True, text=True)
        out = p.stdout or ""
        err = p.stderr or ""

        body, sep, status_tail = out.rpartition("HTTPSTATUS:")
        http_status = int(status_tail.strip()) if sep else None

        if p.returncode != 0 or (http_status and http_status >= 400):
            if DEBUG:
                print(f"[curl-err] code={p.returncode} status={http_status} "
                    f"body={body[:300]!r} stderr={err.strip()!r}")
            raise RuntimeError(f"curl failed (status={http_status}, code={p.returncode}): "
                            f"{(body or err).strip()}")

        if DEBUG:
            print(f"[curl-ok] status={http_status} body[0:120]={body[:120]!r}")

        return json.loads(body or "{}")

    # ---------- token ----------
    def _curl_token(self) -> str:
        out = self._curl_json(
            self._h_common()
            + ["-X", "POST", f"{self.base}/api/auth/token",
               "-H", "Content-Type: application/json",
               "--data-raw", "{}"]
        )
        tid = (out or {}).get("id")
        if DEBUG: print("[auth.token] ->", tid)
        if not tid:
            raise RuntimeError("No id from /api/auth/token")
        return tid

    # ---------- /api/auth/type -> salt ----------
    def _curl_type(self, token_id: str) -> str:
        x = f"{token_id}:711"  # small numeric suffix like the UI
        out = self._curl_json(
            self._h_common()
            + ["-X", "POST", f"{self.base}/api/auth/type",
               "-H", "X-Requested-With: XMLHttpRequest",
               "-H", "Content-Type: text/plain; charset=UTF-8",
               "-H", f"X-Token: {x}",
               "--data-raw", json.dumps({"username": self.username}, separators=(",", ":"))]
        )
        salt_b64 = (out or {}).get("salt", "")
        if DEBUG: print("[auth.type] salt len:", len(salt_b64))
        if not salt_b64:
            raise RuntimeError("No 'salt' from /api/auth/type")
        return salt_b64

    def _argon2id_key32(self, salt_b64: str) -> str:
        salt = base64.b64decode(salt_b64)
        k = hash_secret_raw(self.password.encode(), salt,
                            time_cost=2, memory_cost=65536, parallelism=1,
                            hash_len=32, type=Argon2Type.ID)
        key = _b64(k)
        if DEBUG: print("[derive] argon2id key32:", key[:8], "…")
        return key

    # ---------- /api/auth/authentication ----------
    def _curl_authentication(self, token_id: str, key32: str) -> str | None:
        """
        Try the 4 permutations that cover edge cases:
        1) h1, X-Token header + token in body
        2) h1, token in body only
        3) h2, X-Token header + token in body
        4) h2, X-Token header only
        Return session id on success; None otherwise.
        """
        attempts = [
            # (label, http2, send_header, token_in_body)
            ("h1/hdr+body", False, True,  True),
            ("h1/body-only", False, False, True),
            ("h2/hdr+body", True,  True,  True),
            ("h2/hdr-only", True,  True,  False),
        ]

        for label, use_http2, use_hdr, token_in_body in attempts:
            headers = self._h_common() + [
                "-H", "X-Requested-With: XMLHttpRequest",
                "-H", "Content-Type: text/plain; charset=UTF-8",
            ]
            if use_hdr:
                headers += ["-H", f"X-Token: {token_id}:733"]

            payload = {
                "tid": 0,
                "username": self.username,
                "password": key32.strip(),
                "authcode": ""
            }
            if token_in_body:
                payload["token"] = token_id

            try:
                out = self._curl_json(
                    headers + [
                        "-X", "POST", f"{self.base}/api/auth/authentication",
                        "--data-raw", json.dumps(payload, separators=(",", ":"))
                    ],
                    http2=use_http2
                )
                sid = (out or {}).get("id")
                if DEBUG:
                    print(f"[auth.authentication] {label} -> {'OK' if sid else 'FAILED'}")
                if sid:
                    return sid
            except Exception as e:
                if DEBUG:
                    print(f"[auth.authentication] {label} exception:", e)
                # try next permutation

        return None

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

    # ---------- login orchestrator (unchanged logic, but uses the cURL helpers) ----------
    def login(self) -> str:
        # reuse any valid X-Session
        if self.s.headers.get("X-Session"):
            r = self.s.get(f"{self.base}/api/recipient/rtips")
            if r.status_code == 200:
                return self.s.headers["X-Session"]
            self.s.headers.pop("X-Session", None)

        t1 = self._curl_token()
        salt_b64 = self._curl_type(t1)

        # Argon2id (the one you proved works)
        from argon2.low_level import hash_secret_raw, Type as Argon2Type
        import base64
        k = hash_secret_raw(
            self.password.encode(), base64.b64decode(salt_b64),
            time_cost=2, memory_cost=65536, parallelism=1,
            hash_len=32, type=Argon2Type.ID
        )
        key32 = base64.b64encode(k).decode()

        # Try with a fresh token first (matches your manual flow)
        t2 = self._curl_token()
        sid = self._curl_authentication(t2, key32)
        if not sid:
            # Some nodes prefer the same token id as /type (t1)
            sid = self._curl_authentication(t1, key32)
        if not sid:
            # As an absolute fallback, force http/1.1 again with a new token:
            t3 = self._curl_token()
            sid = self._curl_authentication(t3, key32)

        if not sid:
            raise RuntimeError("Login refused by /api/auth/authentication (via cURL/http1.1).")

        # keep for subsequent GETs
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
