# enhanced_globaleaks_client.py - Improved error handling and debugging
import os, json, base64, random, subprocess, shlex, shutil, requests
from typing import Any, Dict, List, Optional, Sequence
from argon2.low_level import hash_secret_raw, Type as Argon2Type
import time
import tempfile
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = (os.getenv("GLOBALEAKS_BASE_URL", "") or "").rstrip("/")
USER = os.getenv("GLOBALEAKS_USERNAME", "")
PASS = os.getenv("GLOBALEAKS_PASSWORD", "")
AUTHCODE = os.getenv("GLOBALEAKS_AUTHCODE", "")  # usually empty
DEBUG = os.getenv("GLOBALEAKS_DEBUG", "0") not in ("", "0", "false", "False")
CURL_BIN = os.getenv("CURL_BIN") or shutil.which("curl") or "curl"

# Increased timeouts for better reliability
CONNECT_TIMEOUT = int(os.getenv("GL_CONNECT_TIMEOUT", "30"))
MAX_TIMEOUT = int(os.getenv("GL_MAX_TIMEOUT", "60"))
RETRY_TOTAL = int(os.getenv("GL_RETRIES", "3"))

def _ua():
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36")

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")

class GlobaLeaksClient:
    def __init__(self, base: str, username: str, password: str, authcode: str = AUTHCODE):
        if not base:
            raise RuntimeError("GLOBALEAKS_BASE_URL is empty")
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.authcode = authcode or ""
        
        # Parse the base URL to get hostname for debugging
        parsed = urlparse(self.base)
        self.hostname = parsed.hostname or "unknown"

        self.s = requests.Session()
        self.s.trust_env = False
        self.s.headers.update({"User-Agent": _ua()})

        # Increase default timeouts for requests session
        self.s.timeout = (CONNECT_TIMEOUT, MAX_TIMEOUT)

        sid_env = (os.getenv("GLOBALEAKS_SESSION_ID", "") or "").strip()
        if sid_env:
            self.s.headers["X-Session"] = sid_env
            if DEBUG:
                print(f"[init] Using existing session: {sid_env[:8]}...")

        if DEBUG:
            print(f"[init] curl at: {CURL_BIN}")
            print(f"[init] base: {self.base}")
            print(f"[init] username: {self.username}")
            print(f"[init] timeouts: connect={CONNECT_TIMEOUT}s, max={MAX_TIMEOUT}s")

    def _h_common(self) -> list[str]:
        base = self.base
        return [
            "-H", "Accept: application/json, text/plain, */*",
            "-H", f"Origin: {base}",
            "-H", f"Referer: {base}/#/login",
            "-H", f"User-Agent: {_ua()}",
            "--connect-timeout", str(CONNECT_TIMEOUT),
            "--max-time", str(MAX_TIMEOUT),
            "--retry", "2",  # Add retry logic
            "--retry-delay", "1",
        ]

    def _h(self, *, ctype: Optional[str] = None, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base,
            "Referer": f"{self.base}/#/login",
            "X-Requested-With": "XMLHttpRequest",
        }
        if ctype:
            headers["Content-Type"] = ctype
        if extra:
            headers.update(extra)
        return headers

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
            print("[curl]", " ".join(shlex.quote(a) for a in full))

        start_time = time.time()
        try:
            p = subprocess.run(full, capture_output=True, text=True, timeout=MAX_TIMEOUT + 10)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start_time
            raise RuntimeError(f"curl command timed out after {elapsed:.1f}s")
        
        elapsed = time.time() - start_time
        out = p.stdout or ""
        err = p.stderr or ""

        body, sep, status_tail = out.rpartition("HTTPSTATUS:")
        http_status = int(status_tail.strip()) if sep else None

        if DEBUG:
            print(f"[curl] elapsed: {elapsed:.2f}s, return_code: {p.returncode}, http_status: {http_status}")

        if p.returncode != 0 or (http_status and http_status >= 400):
            error_msg = f"curl failed (elapsed: {elapsed:.1f}s, status={http_status}, code={p.returncode})"
            
            if p.returncode == 28:  # curl timeout
                error_msg += " - CONNECTION TIMEOUT"
            elif p.returncode == 7:  # couldn't connect
                error_msg += " - COULDN'T CONNECT TO HOST"
            elif p.returncode == 6:  # couldn't resolve host
                error_msg += " - COULDN'T RESOLVE HOST"
            
            if DEBUG:
                error_msg += f"\nbody: {body[:300]!r}\nstderr: {err.strip()!r}"
            
            # Include some diagnostics in the error
            if "timeout" in err.lower() or p.returncode == 28:
                error_msg += f"\n\nDIAGNOSTIC HINTS:"
                error_msg += f"\n- Check if {self.hostname} is reachable"
                error_msg += f"\n- Try increasing timeouts: GL_CONNECT_TIMEOUT={CONNECT_TIMEOUT*2} GL_MAX_TIMEOUT={MAX_TIMEOUT*2}"
                error_msg += f"\n- Test with: curl -I --connect-timeout {CONNECT_TIMEOUT} {self.base}"
            
            raise RuntimeError(error_msg)

        if DEBUG and elapsed > 5:
            print(f"[curl-slow] request took {elapsed:.2f}s - consider checking network")

        if DEBUG:
            print(f"[curl-ok] status={http_status} body[0:120]={body[:120]!r}")

        return json.loads(body or "{}")

    def _test_connectivity(self) -> bool:
        """Test basic connectivity to the GlobaLeaks server"""
        if DEBUG:
            print(f"[connectivity] Testing connection to {self.base}")
        
        try:
            r = self.s.get(f"{self.base}/api/auth/token", 
                          headers=self._h(ctype="application/json"), 
                          timeout=(CONNECT_TIMEOUT, MAX_TIMEOUT))  # Short timeout for connectivity test
            if DEBUG:
                print(f"[connectivity] HTTP {r.status_code} in {r.elapsed.total_seconds():.2f}s")
            return r.status_code < 500
        except requests.exceptions.ConnectTimeout:
            if DEBUG:
                print(f"[connectivity] CONNECT TIMEOUT to {self.hostname}")
            return False
        except requests.exceptions.ReadTimeout:
            if DEBUG:
                print(f"[connectivity] READ TIMEOUT from {self.hostname}")
            return False
        except requests.exceptions.ConnectionError as e:
            if DEBUG:
                print(f"[connectivity] CONNECTION ERROR: {e}")
            return False
        except Exception as e:
            if DEBUG:
                print(f"[connectivity] UNEXPECTED ERROR: {e}")
            return False

    def _curl_token(self) -> str:
        try:
            out = self._curl_json(
                self._h_common()
                + ["-X", "POST", f"{self.base}/api/auth/token",
                   "-H", "Content-Type: application/json",
                   "--data-raw", "{}"]
            )
        except RuntimeError as e:
            if "timeout" in str(e).lower() or "couldn't connect" in str(e).lower():
                # Suggest fallback strategies
                raise RuntimeError(f"Failed to get auth token: {e}\n\n"
                                 f"TROUBLESHOOTING STEPS:\n"
                                 f"1. Verify server is accessible: curl -I {self.base}\n"
                                 f"2. Check if using correct URL (http vs https)\n"
                                 f"3. Test with onion address if available\n"
                                 f"4. Increase timeouts with GL_CONNECT_TIMEOUT and GL_MAX_TIMEOUT")
            raise
        
        tid = (out or {}).get("id")
        if DEBUG: 
            print(f"[auth.token] -> {tid[:8] if tid else 'None'}...")
        if not tid:
            raise RuntimeError(f"No id from /api/auth/token. Response: {out}")
        return tid

    def _curl_type(self, token_id: str) -> tuple[str, str]:
        suffixes = ["711", "733", "299", "180"]
        errors = []
        
        for suffix in suffixes:
            try:
                out = self._curl_json(
                    self._h_common()
                    + ["-X", "POST", f"{self.base}/api/auth/type",
                       "-H", "X-Requested-With: XMLHttpRequest",
                       "-H", "Content-Type: text/plain; charset=UTF-8",
                       "-H", f"X-Token: {token_id}:{suffix}",
                       "--data-raw", json.dumps({"username": self.username}, separators=(",", ":"))]
                )
                salt_b64 = (out or {}).get("salt", "")
                if DEBUG:
                    print(f"[auth.type] suffix={suffix} -> salt len: {len(salt_b64)}")
                if salt_b64:
                    return salt_b64, suffix
                else:
                    errors.append(f"suffix {suffix}: no salt in response {out}")
            except Exception as exc:
                errors.append(f"suffix {suffix}: {exc}")
                continue
        
        error_summary = "; ".join(errors)
        raise RuntimeError(f"No 'salt' from /api/auth/type after trying all suffixes. Errors: {error_summary}")

    def _argon2id_key32(self, salt_b64: str) -> str:
        try:
            salt = base64.b64decode(salt_b64)
        except Exception as e:
            raise RuntimeError(f"Failed to decode salt base64 '{salt_b64}': {e}")
        
        try:
            k = hash_secret_raw(self.password.encode(), salt,
                                time_cost=2, memory_cost=65536, parallelism=1,
                                hash_len=32, type=Argon2Type.ID)
            key = _b64(k)
            if DEBUG: 
                print(f"[derive] argon2id key32: {key[:8]}...")
            return key
        except Exception as e:
            raise RuntimeError(f"Failed to derive Argon2ID key: {e}")

    def _curl_authentication(self, token_id: str, key32: str, suffixes: Sequence[str]) -> Optional[str]:
        """
        Try authentication with multiple strategies
        """
        attempts = [
            # (label, http2, send_header, token_in_body)
            ("h1/hdr+body", False, True,  True),
            ("h1/body-only", False, False, True),
            ("h2/hdr+body", True,  True,  True),
            ("h2/hdr-only", True,  True,  False),
        ]

        for suffix in suffixes:
            for label, use_http2, use_hdr, token_in_body in attempts:
                headers = self._h_common() + [
                    "-H", "X-Requested-With: XMLHttpRequest",
                    "-H", "Content-Type: text/plain; charset=UTF-8",
                ]
                if use_hdr:
                    headers += ["-H", f"X-Token: {token_id}:{suffix}"]

                payload = {
                    "tid": 0,
                    "username": self.username,
                    "password": key32.strip(),
                }
                if self.authcode:
                    payload["authcode"] = self.authcode
                if token_in_body:
                    payload["token"] = token_id

                try:
                    if DEBUG:
                        print(f"[auth.authentication] trying {label} suffix={suffix}...")
                    
                    out = self._curl_json(
                        headers + [
                            "-X", "POST", f"{self.base}/api/auth/authentication",
                            "--data-raw", json.dumps(payload, separators=(",", ":"))
                        ],
                        http2=use_http2
                    )
                    
                    sid = (out or {}).get("id")
                    if DEBUG:
                        status = "SUCCESS" if sid else f"FAILED - response: {out}"
                        print(f"[auth.authentication] {label} suffix={suffix} -> {status}")
                    if sid:
                        return sid
                except Exception as e:
                    if DEBUG:
                        print(f"[auth.authentication] {label} suffix={suffix} exception: {e}")
                    continue

        return None

    def _curl_session_login(self, token_id: str, key32_b64: str, suffixes: Sequence[str]) -> Optional[str]:
        payload = {
            "role": "recipient",
            "username": self.username,
            "password": key32_b64,
            "hashing": "argon2id",
            "token": token_id,
        }
        if self.authcode:
            payload["authcode"] = self.authcode

        for suffix in suffixes:
            try:
                if DEBUG:
                    print(f"[auth.session] trying suffix={suffix}...")
                
                out = self._curl_json([
                    "-X", "POST", f"{self.base}/api/auth/session",
                    "-H", "Accept: application/json, text/plain, */*",
                    "-H", "X-Requested-With: XMLHttpRequest",
                    "-H", f"Referer: {self.base}/#/login",
                    "-H", f"Origin: {self.base}",
                    "-H", "Content-Type: application/json",
                    "-H", f"X-Token: {token_id}:{suffix}",
                    "--data", json.dumps(payload, separators=(",", ":")),
                ])
                
                sid = out.get("id")
                if DEBUG:
                    status = "SUCCESS" if sid else f"FAILED - response: {out}"
                    print(f"[auth.session] suffix={suffix} -> {status}")
                if sid:
                    return sid
            except Exception as exc:
                if DEBUG:
                    print(f"[auth.session] suffix={suffix} exception: {exc}")
                continue
        return None

    def _validate_session(self) -> bool:
        try:
            r = self.s.get(f"{self.base}/api/recipient/rtips", 
                          headers=self._h(ctype="application/json"), 
                          timeout=(10, 20))
            if DEBUG:
                print(f"[validate] session check HTTP {r.status_code}")
            return r.status_code == 200
        except Exception as e:
            if DEBUG:
                print(f"[validate] session check failed: {e}")
            return False

    def login(self) -> str:
        """Enhanced login with better error reporting"""
        if DEBUG:
            print("[login] Starting login process...")
        
        # Test basic connectivity first
        if not self._test_connectivity():
            raise RuntimeError(f"Cannot connect to GlobaLeaks server at {self.base}. "
                             f"Please check the URL and network connectivity.")
        
        # reuse any valid X-Session
        if self.s.headers.get("X-Session"):
            if DEBUG:
                print("[login] Checking existing session...")
            if self._validate_session():
                if DEBUG:
                    print("[login] Existing session is valid")
                return self.s.headers["X-Session"]
            else:
                if DEBUG:
                    print("[login] Existing session invalid, removing")
                self.s.headers.pop("X-Session", None)

        try:
            t_type = self._curl_token()
            salt_b64, salt_suffix = self._curl_type(t_type)
            key32 = self._argon2id_key32(salt_b64)

            auth_suffixes = ["299", "733", "711", "180"]
            if salt_suffix and salt_suffix not in auth_suffixes:
                auth_suffixes.insert(0, salt_suffix)
            elif salt_suffix:
                try:
                    auth_suffixes.insert(0, auth_suffixes.pop(auth_suffixes.index(salt_suffix)))
                except ValueError:
                    pass

            sid: Optional[str] = None
            tokens_to_try = [t_type, self._curl_token()]  # Try the type token first
            seen_tokens = set()
            
            while tokens_to_try and not sid:
                token_id = tokens_to_try.pop(0)
                if token_id in seen_tokens:
                    continue
                seen_tokens.add(token_id)
                
                if DEBUG:
                    print(f"[login] Trying authentication with token {token_id[:8]}...")
                
                sid = self._curl_authentication(token_id, key32, auth_suffixes)
                if sid:
                    break
                    
                if DEBUG:
                    print(f"[login] Trying session login with token {token_id[:8]}...")
                    
                sid = self._curl_session_login(token_id, key32, auth_suffixes)
                if sid:
                    break
                
                # Generate one more token if we've exhausted the current ones
                if len(seen_tokens) <= 2 and len(tokens_to_try) == 0:
                    try:
                        new_token = self._curl_token()
                        if new_token not in seen_tokens:
                            tokens_to_try.append(new_token)
                    except Exception as e:
                        if DEBUG:
                            print(f"[login] Failed to get additional token: {e}")
                        break

            if not sid:
                error_msg = (f"Login failed to GlobaLeaks node at {self.base}\n\n"
                           f"ATTEMPTED STRATEGIES:\n"
                           f"- Multiple token variations\n" 
                           f"- HTTP/1.1 and HTTP/2 protocols\n"
                           f"- Different authentication endpoints\n"
                           f"- Various suffix combinations: {auth_suffixes}\n\n"
                           f"POSSIBLE CAUSES:\n"
                           f"- Incorrect username/password\n"
                           f"- 2FA required but not provided\n"
                           f"- Server-side authentication changes\n"
                           f"- Network/proxy interference\n\n"
                           f"TROUBLESHOOTING:\n"
                           f"- Verify credentials in web browser\n"
                           f"- Check if 2FA is enabled\n"
                           f"- Try with onion address if available\n"
                           f"- Enable DEBUG=1 for detailed logs")
                raise RuntimeError(error_msg)

            # keep for subsequent GETs
            self.s.headers["X-Session"] = sid
            if DEBUG:
                print(f"[login] SUCCESS - session: {sid[:8]}...")
            return sid
            
        except RuntimeError:
            raise  # Re-raise our custom errors as-is
        except Exception as e:
            raise RuntimeError(f"Unexpected error during login: {e}") from e

    def get(self, path: str) -> requests.Response:
        if not self.s.headers.get("X-Session"):
            self.login()
        
        try:
            r = self.s.get(f"{self.base}{path}", 
                          headers=self._h(ctype="application/json"), 
                          timeout=(10, 20))
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as e:
            raise RuntimeError(f"Timeout accessing {path}: {e}")
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"Connection error accessing {path}: {e}")
        
        if r.status_code == 412:  # Session expired
            if DEBUG:
                print(f"[get] Session expired (412), re-authenticating...")
            self.s.headers.pop("X-Session", None)
            self.login()
            try:
                r = self.s.get(f"{self.base}{path}", 
                              headers=self._h(ctype="application/json"), 
                              timeout=(CONNECT_TIMEOUT, MAX_TIMEOUT))
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as e:
                raise RuntimeError(f"Timeout accessing {path} after re-auth: {e}")
        
        r.raise_for_status()
        return r

    def list_tips(self) -> List[Dict[str, Any]]:
        response = self.get("/api/recipient/rtips")
        tips = response.json() or []
        if DEBUG:
            print(f"[list_tips] Retrieved {len(tips)} tips")
        return tips
    
class TorGlobaLeaksClient(GlobaLeaksClient):
    def __init__(self, base, username, password, authcode=""):
        use_onion = os.getenv("USE_ONION", "0") == "1"
        tor_required = os.getenv("TOR_REQUIRED", "0") == "1"  # set to 1 to forbid fallback
        onion_base = os.getenv("GLOBALEAKS_ONION", "").rstrip("/")
        clearnet_base = os.getenv("GLOBALEAKS_BASE_URL", "").rstrip("/")
        tor_socks = os.getenv("TOR_SOCKS", "127.0.0.1:9050")
        
        if use_onion and onion_base:
            print(f"[tor] Using onion address: {onion_base}")
            base = onion_base.rstrip("/")
        
        super().__init__(base, username, password, authcode)
        
        # Configure Tor proxy for requests session
        if use_onion:
            self.s.proxies = {
                'http': f'socks5h://{tor_socks}',
                'https': f'socks5h://{tor_socks}'
            }
            print(f"[tor] Using Tor proxy: {tor_socks}")

        self._tor_required = tor_required
        self._clearnet_base = clearnet_base
        self._onion_base = onion_base
        self._use_onion = use_onion

    def get(self, path: str):
        try:
            return super().get(path)
        except RuntimeError as e:
            # Only fallback if allowed and onion was in use
            if (self._use_onion and not self._tor_required and self._clearnet_base):
                print("[tor] Onion/tor failed, falling back to clearnet for this request")
                self.base = self._clearnet_base
                self.s.proxies = {}  # disable tor
                return super().get(path)
            raise
    
    def _h_common(self) -> list[str]:
        """Override to add Tor proxy support to curl commands"""
        base_args = super()._h_common()
        
        use_onion = os.getenv("USE_ONION", "0") == "1"
        tor_socks = os.getenv("TOR_SOCKS", "127.0.0.1:9050")
        
        if use_onion:
            base_args.extend(["--proxy", f"socks5h://{tor_socks}"])
        
        return base_args

# convenience function
def list_tips_tor():
    base = os.getenv("GLOBALEAKS_BASE_URL", "").rstrip("/")
    user = os.getenv("GLOBALEAKS_USERNAME", "")
    password = os.getenv("GLOBALEAKS_PASSWORD", "")
    authcode = os.getenv("GLOBALEAKS_AUTHCODE", "")
    
    client = TorGlobaLeaksClient(base, user, password, authcode)
    return client.list_tips()

def list_tips() -> List[Dict[str, Any]]:
    return GlobaLeaksClient(BASE, USER, PASS, AUTHCODE).list_tips()

def test_connection():
    try:
        tips = list_tips()
        print(f"✅ Successfully retrieved {len(tips)} tips")
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

if __name__ == "__main__":
    test_connection()