#!/usr/bin/env bash
set -euo pipefail

# ======================= Config (env overrides welcome) =======================
BASE="${GLOBALEAKS_BASE_URL:-https://tips.indyleaks.com}"
USER="${GLOBALEAKS_USERNAME:?set GLOBALEAKS_USERNAME}"
PASS="${GLOBALEAKS_PASSWORD:?set GLOBALEAKS_PASSWORD}"
AUTHCODE="${GLOBALEAKS_AUTHCODE:-}"   # leave empty if no 2FA

# Optional: set USE_ONION=1 and GLOBALEAKS_ONION + TOR_SOCKS (default 127.0.0.1:9050)
USE_ONION="${USE_ONION:-0}"
ONION_BASE="${GLOBALEAKS_ONION:-}"
TOR_SOCKS="${TOR_SOCKS:-127.0.0.1:9050}"

UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
DEBUG="${GLOBALEAKS_DEBUG:-0}"

# Increased timeouts for better reliability
CONNECT_TIMEOUT="${GL_CONNECT_TIMEOUT:-15}"
MAX_TIMEOUT="${GL_MAX_TIMEOUT:-30}"

JAR="$(mktemp -t gl.cookies.XXXXXX)"
HOST="$(printf '%s\n' "${BASE}" | sed -E 's#^https?://([^/]+)/?.*$#\1#')"
echo "Cookie jar: $JAR"
echo "Timeouts: connect=${CONNECT_TIMEOUT}s, max=${MAX_TIMEOUT}s"

# ============================ Curl setup & helpers ============================
# Increased timeouts and retries
CURL_T=(--http1.1 --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIMEOUT" --retry 2 --retry-delay 1)

# Optional Tor proxy (only if USE_ONION=1)
if [[ "$USE_ONION" == "1" ]]; then
  if [[ -z "$ONION_BASE" ]]; then
    echo "USE_ONION=1 but GLOBALEAKS_ONION not set (e.g. http://xxx.onion)"; exit 2
  fi
  BASE="$ONION_BASE"
  CURL_T+=(--proxy "socks5h://${TOR_SOCKS}")
  echo "Using Tor proxy: ${TOR_SOCKS}"
fi

HC=(-H "accept: application/json, text/plain, */*"
    -H "origin: $BASE"
    -H "referer: $BASE/#/login"
    -A "$UA")

# Enhanced connectivity test
test_connectivity() {
  echo "→ Testing connectivity to ${HOST}..."
  if curl -I --connect-timeout 5 --max-time 10 --retry 1 "${BASE}" >/dev/null 2>&1; then
    echo "   ✓ Connection successful"
    return 0
  else
    echo "   ✗ Connection failed"
    echo "   Please check:"
    echo "     • Is ${HOST} reachable from your network?"
    echo "     • Is the URL correct? (http vs https)"
    echo "     • Are you behind a firewall/proxy that blocks the connection?"
    return 1
  fi
}

post_json() {  # $1=url  $2=data  [extra curl args...]
  local url="$1"; local data="$2"; shift 2 || true
  local start_time=$(date +%s)
  
  if ! curl -sS "${CURL_T[@]}" -c "$JAR" "${HC[@]}" \
       -H 'content-type: application/json' \
       "$@" -X POST "$url" --data "$data"; then
    local exit_code=$?
    local elapsed=$(($(date +%s) - start_time))
    echo "   ✗ curl failed (exit code: $exit_code, elapsed: ${elapsed}s)"
    case $exit_code in
      28) echo "   Connection timed out after ${elapsed}s" ;;
      7)  echo "   Couldn't connect to host ${HOST}" ;;
      6)  echo "   Couldn't resolve host ${HOST}" ;;
      *)  echo "   Curl error code: $exit_code" ;;
    esac
    return $exit_code
  fi
}

post_plain() { # $1=url  $2=data  $3=x-token(opt)  [extra curl args...]
  local url="$1"; local data="$2"; local xtoken="${3:-}"; shift 3 || true
  local start_time=$(date +%s)
  
  if ! curl -sS "${CURL_T[@]}" -c "$JAR" "${HC[@]}" \
       -H 'x-requested-with: XMLHttpRequest' \
       -H 'content-type: text/plain; charset=UTF-8' \
       ${xtoken:+-H "X-Token: $xtoken"} \
       "$@" -X POST "$url" --data "$data"; then
    local exit_code=$?
    local elapsed=$(($(date +%s) - start_time))
    echo "   ✗ curl failed (exit code: $exit_code, elapsed: ${elapsed}s)"
    return $exit_code
  fi
}

dump_head_body() {  # pretty-print an HTTP response (from curl -D - output)
  awk 'NR==1, NF==0{print; next} {print >"/dev/stderr"}'  # header to stdout, body to stderr
}

# Test connectivity first
test_connectivity || {
  echo
  echo "TROUBLESHOOTING SUGGESTIONS:"
  echo "1. Check if the server URL is correct: ${BASE}"
  echo "2. Try accessing the web interface in a browser"
  echo "3. If using Tor, try: USE_ONION=1 GLOBALEAKS_ONION=<onion-url>"
  echo "4. Increase timeouts: GL_CONNECT_TIMEOUT=30 GL_MAX_TIMEOUT=60"
  echo "5. Check network connectivity: ping ${HOST}"
  exit 1
}

# =============================== 1) /auth/token ===============================
echo "→ 1) /auth/token"
if ! T="$(post_json "$BASE/api/auth/token" '{}' | jq -r .id)"; then
  echo "   Failed to get auth token"
  exit 1
fi

if [[ -z "$T" || "$T" == "null" ]]; then
  echo "   ✗ No token received from server"
  exit 1
fi

echo "   ✓ token = ${T:0:8}…"

# Always send token cookie explicitly (some front-ends don't Set-Cookie)
COOKIE_SEND=(-b "token=$T")
# Also write to jar (not strictly required)
printf '%s\tFALSE\t/\tTRUE\t0\ttoken\t%s\n' "$HOST" "$T" >> "$JAR"

# ========================= 2) /auth/type (get salt) ==========================
echo "→ 2) /auth/type (salt)"
TYPE_SFX=711
SALT_B64=""

get_salt_with_suffix() {
  local suffix="$1"
  if SALT_B64="$(post_plain "$BASE/api/auth/type" "{\"username\":\"$USER\"}" "$T:$suffix" "${COOKIE_SEND[@]}" 2>/dev/null | jq -r .salt 2>/dev/null)"; then
    if [[ -n "$SALT_B64" && "$SALT_B64" != "null" ]]; then
      TYPE_SFX="$suffix"
      return 0
    fi
  fi
  return 1
}

# Try different suffixes
for s in 711 733 299 180; do
  if get_salt_with_suffix "$s"; then
    break
  fi
done

if [[ -z "${SALT_B64:-}" || "$SALT_B64" == "null" ]]; then
  echo "   ✗ Failed to get salt from server with all suffixes"
  echo "   This could indicate:"
  echo "     • Invalid username: $USER"
  echo "     • Server authentication method changed"
  echo "     • Network/proxy interference"
  exit 1
fi

echo "   ✓ salt len = ${#SALT_B64} (suffix :$TYPE_SFX)"

# ========================== 3) Argon2id derive key ===========================
echo "→ 3) Argon2id (32B → base64)"
if ! KEY32="$(
  SALT_B64="$SALT_B64" PASS="$PASS" python3 - <<'PY'
import os, base64
try:
    from argon2.low_level import hash_secret_raw, Type
    s = base64.b64decode(os.environ["SALT_B64"])
    pw = os.environ["PASS"].encode()
    k  = hash_secret_raw(pw, s, time_cost=2, memory_cost=65536, parallelism=1, hash_len=32, type=Type.ID)
    print(base64.b64encode(k).decode().strip())
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    exit(1)
PY
)"; then
  echo "   ✗ Key derivation failed"
  echo "   Make sure python3-argon2 is installed: pip install argon2-cffi"
  exit 1
fi

if [[ -z "$KEY32" ]]; then
  echo "   ✗ Key derivation returned empty result"
  exit 1
fi

echo "   ✓ key32 b64 len = ${#KEY32}"

# ========================= 4) /auth/authentication ===========================
echo "→ 4) /auth/authentication"

SID=""
AUTH_SFX=""

try_auth() {  # $1=ctype  $2=suffix  $3=with_hashing y/n  $4=with_xrw y/n  $5=token_in_body y/n  $6=tid_value (0 or "0")
  local ctype="$1" sfx="$2" wh="$3" xrw="$4" tib="$5" tidv="$6"
  local body='{"tid":'"$tidv"',"username":"'"$USER"'","password":"'"$KEY32"'"'
  [[ -n "$AUTHCODE" ]] && body="$body,\"authcode\":\"$AUTHCODE\""
  [[ "$wh" == "y" ]] && body="$body,\"hashing\":\"argon2id\""
  [[ "$tib" == "y" ]] && body="$body,\"token\":\"$T:$sfx\""
  body="$body}"

  local args=("${CURL_T[@]}" -c "$JAR" "${COOKIE_SEND[@]}" "${HC[@]}" -H "Content-Type: $ctype" -H "X-Token: $T:$sfx")
  [[ "$xrw" == "y" ]] && args+=(-H 'X-Requested-With: XMLHttpRequest')
  # extra hardening
  args+=(-H 'Expect:' -H 'Connection: close')

  # request with timeout handling
  local resp code body_json start_time elapsed
  start_time=$(date +%s)
  
  if ! resp="$(curl -sS -D - "${args[@]}" -X POST "$BASE/api/auth/authentication" --data "$body" 2>/dev/null)"; then
    elapsed=$(($(date +%s) - start_time))
    echo "   try ctype='$ctype' suffix=:${sfx} hashing=$wh xrw=$xrw token_in_body=$tib tid=$tidv → TIMEOUT/ERROR (${elapsed}s)"
    return 1
  fi
  
  elapsed=$(($(date +%s) - start_time))
  code="$(printf '%s' "$resp" | awk 'NR==1{print $2}')"
  body_json="$(printf '%s' "$resp" | sed '1,/^\r\{0,1\}$/d')"

  echo "   try ctype='$ctype' suffix=:${sfx} hashing=$wh xrw=$xrw token_in_body=$tib tid=$tidv → HTTP ${code:-<failed>} (${elapsed}s)"
  [[ "$DEBUG" = 1 ]] && echo "     body: $(printf '%s' "$body_json" | head -c 240)…"

  if [[ "$code" =~ ^20(0|1)$ ]]; then
    SID="$(printf '%s' "$body_json" | jq -r '.id // empty' 2>/dev/null || true)"
    [[ -n "${SID:-}" ]] && { AUTH_SFX="$sfx"; return 0; }
  fi
  return 1
}

# More focused matrix to reduce timeout exposure
echo "   Trying authentication variants..."
for ctype in 'text/plain; charset=UTF-8' 'application/json'; do
  for sfx in 299 733 711 180; do
    for with_hash in y n; do
      for token_body in y n; do
        # XRW on; both tid int and string
        if try_auth "$ctype" "$sfx" "$with_hash" y "$token_body" 0; then 
          echo "   ✓ Authentication successful!"
          break 4
        fi
        if try_auth "$ctype" "$sfx" "$with_hash" y "$token_body" '"0"'; then 
          echo "   ✓ Authentication successful!"
          break 4
        fi
      done
    done
  done
done

if [[ -z "${SID:-}" ]]; then
  echo
  echo "✗ Authentication failed across all tried variants."
  echo
  echo "POSSIBLE CAUSES:"
  echo "  • Incorrect username or password"
  echo "  • 2FA required but GLOBALEAKS_AUTHCODE not set"
  echo "  • Server-side authentication changes"
  echo "  • Reverse proxy stripping headers on /api/auth/authentication"
  echo
  echo "TROUBLESHOOTING STEPS:"
  echo "  1. Verify credentials work in the web browser"
  echo "  2. Check if 2FA is enabled: export GLOBALEAKS_AUTHCODE=123456"
  echo "  3. Try via onion address: USE_ONION=1 GLOBALEAKS_ONION=<onion-url>"
  echo "  4. Test with increased timeouts: GL_CONNECT_TIMEOUT=30 GL_MAX_TIMEOUT=60"
  echo "  5. Enable debug mode: GLOBALEAKS_DEBUG=1"
  echo
  if [[ "$USE_ONION" != "1" ]]; then
    echo "  Quick onion test (if available):"
    echo "    USE_ONION=1 GLOBALEAKS_ONION=<your-onion-url> $0"
    echo
  fi
  exit 1
fi

echo "   ✓ X-Session = ${SID:0:16}… (auth :$AUTH_SFX)"

# ====================== 5) /auth/session (finalize) ==========================
echo "→ 5) /auth/session (finalize)"
OK=0
SESS_SFX=""
for sfx in 180 299 733 711; do
  start_time=$(date +%s)
  code="$(curl -sS "${CURL_T[@]}" -o /dev/null -w '%{http_code}' "${COOKIE_SEND[@]}" "${HC[@]}" \
           -H 'content-type: application/json' -H 'Expect:' -H 'Connection: close' \
           -H "X-Session: $SID" \
           -X POST "$BASE/api/auth/session" \
           --data "{\"role\":\"receiver\",\"token\":\"$T:$sfx\"}" 2>/dev/null || echo "000")"
  elapsed=$(($(date +%s) - start_time))
  echo "   finalize with :$sfx → HTTP $code (${elapsed}s)"
  if [[ "$code" == "200" || "$code" == "201" || "$code" == "204" ]]; then
    OK=1; SESS_SFX="$sfx"; break
  fi
done

if [[ "$OK" != 1 ]]; then
  echo "   ✗ Session finalize failed"
  echo "   This suggests the authentication was partial but session creation failed"
  exit 3
fi

echo "   ✓ Session finalized (:${SESS_SFX})"

# ============================== 6) sanity test ===============================
echo "→ 6) Test: /recipient/rtips"
if tips_count=$(curl -fsS "${CURL_T[@]}" -H "X-Session: $SID" "$BASE/api/recipient/rtips" 2>/dev/null | jq length 2>/dev/null); then
  echo "   ✓ Retrieved $tips_count tips"
  echo
  echo "SUCCESS! Session ID: $SID"
  echo "You can use this session with: export GLOBALEAKS_SESSION_ID='$SID'"
else
  echo "   ✗ Failed to retrieve tips (but login appeared to work)"
  echo "   Session ID: $SID"
  exit 4
fi

# Cleanup
rm -f "$JAR" 2>/dev/null || true