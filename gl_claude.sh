#!/usr/bin/env bash
# gl_login_fixed.sh - Fixed version that handles the response format correctly
set -euo pipefail

BASE="${GLOBALEAKS_BASE_URL:-https://tips.indyleaks.com}"
USER="${GLOBALEAKS_USERNAME:?set GLOBALEAKS_USERNAME}"
PASS="${GLOBALEAKS_PASSWORD:?set GLOBALEAKS_PASSWORD}"
AUTHCODE="${GLOBALEAKS_AUTHCODE:-}"

UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
DEBUG="${GLOBALEAKS_DEBUG:-0}"

# Increased timeouts
CONNECT_TIMEOUT=20
MAX_TIMEOUT=60

JAR="$(mktemp -t gl.cookies.XXXXXX)"
HOST="$(printf '%s\n' "${BASE}" | sed -E 's#^https?://([^/]+)/?.*$#\1#')"
echo "Cookie jar: $JAR"
echo "Timeouts: connect=${CONNECT_TIMEOUT}s, max=${MAX_TIMEOUT}s"

# Curl setup with better timeouts
CURL_T=(--http1.1 --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIMEOUT" --retry 2 --retry-delay 2)

HC=(-H "accept: application/json, text/plain, */*"
    -H "origin: $BASE"
    -H "referer: $BASE/#/login"
    -A "$UA")

# Enhanced post functions with better error handling
post_json() {
    local url="$1"; local data="$2"; shift 2 || true
    curl -sS "${CURL_T[@]}" -c "$JAR" -b "$JAR" "${HC[@]}" \
         -H 'content-type: application/json' \
         "$@" -X POST "$url" --data "$data"
}

post_plain() {
    local url="$1"; local data="$2"; local xtoken="${3:-}"; shift 3 || true
    curl -sS "${CURL_T[@]}" -c "$JAR" -b "$JAR" "${HC[@]}" \
         -H 'x-requested-with: XMLHttpRequest' \
         -H 'content-type: text/plain; charset=UTF-8' \
         ${xtoken:+-H "X-Token: $xtoken"} \
         "$@" -X POST "$url" --data "$data"
}

# Clean JSON extraction from mixed output
extract_json() {
    grep -o '{.*}' | tail -n1 2>/dev/null || cat
}

# Test basic connectivity
echo "→ Testing connectivity..."
if ! curl -I --connect-timeout 10 --max-time 15 "$BASE" >/dev/null 2>&1; then
    echo "✗ Cannot connect to $BASE"
    echo "Check your internet connection and the server URL"
    exit 1
fi
echo "✓ Server is reachable"

# Get initial token
echo "→ 1) Getting auth token..."
T="$(post_json "$BASE/api/auth/token" '{}' | extract_json | jq -r .id)"
if [[ -z "$T" || "$T" == "null" ]]; then
    echo "✗ Failed to get token"
    exit 1
fi
echo "✓ Token: ${T:0:8}..."

# Store token in cookie jar
printf '%s\tFALSE\t/\tTRUE\t0\ttoken\t%s\n' "$HOST" "$T" >> "$JAR"
COOKIE_SEND=(-b "$JAR")

# Get salt with multiple suffix attempts
echo "→ 2) Getting salt..."
SALT_B64=""
TYPE_SFX=""

for sfx in 711 733 299 180; do
    echo "  Trying suffix :$sfx..."
    if response=$(post_plain "$BASE/api/auth/type" "{\"username\":\"$USER\"}" "$T:$sfx" "${COOKIE_SEND[@]}" 2>/dev/null); then
        # Extract just the JSON part and get salt
        clean_response=$(echo "$response" | extract_json)
        SALT_B64=$(echo "$clean_response" | jq -r '.salt // empty' 2>/dev/null || echo "")
        
        if [[ -n "$SALT_B64" && "$SALT_B64" != "null" ]]; then
            TYPE_SFX="$sfx"
            echo "  ✓ Got salt with suffix :$sfx (length: ${#SALT_B64})"
            break
        else
            [[ "$DEBUG" == "1" ]] && echo "    No salt in: $clean_response"
        fi
    fi
done

if [[ -z "$SALT_B64" ]]; then
    echo "✗ Failed to get salt with any suffix"
    echo "This usually means credentials are wrong or server config changed"
    exit 1
fi

# Derive key using Argon2id
echo "→ 3) Deriving Argon2id key..."
KEY32="$(
  SALT_B64="$SALT_B64" PASS="$PASS" python3 - <<'PY'
import os, base64
try:
    from argon2.low_level import hash_secret_raw, Type
    s = base64.b64decode(os.environ["SALT_B64"])
    pw = os.environ["PASS"].encode()
    k = hash_secret_raw(pw, s, time_cost=2, memory_cost=65536, parallelism=1, hash_len=32, type=Type.ID)
    print(base64.b64encode(k).decode().strip())
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    exit(1)
PY
)"

if [[ -z "$KEY32" ]]; then
    echo "✗ Key derivation failed"
    echo "Make sure argon2-cffi is installed: pip install argon2-cffi"
    exit 1
fi
echo "✓ Derived key: ${KEY32:0:8}..."

# Authentication
echo "→ 4) Authenticating..."
SID=""

# Build auth payload
auth_payload='{"tid":0,"username":"'"$USER"'","password":"'"$KEY32"'"'
[[ -n "$AUTHCODE" ]] && auth_payload="$auth_payload,\"authcode\":\"$AUTHCODE\""
auth_payload="$auth_payload}"

# Try authentication with the working suffix
echo "  Attempting authentication with suffix :$TYPE_SFX..."

response=$(curl -sS "${CURL_T[@]}" -c "$JAR" -b "$JAR" "${HC[@]}" \
    -H 'content-type: text/plain; charset=UTF-8' \
    -H 'x-requested-with: XMLHttpRequest' \
    -H "X-Token: $T:$TYPE_SFX" \
    -X POST "$BASE/api/auth/authentication" \
    --data "$auth_payload" 2>/dev/null || echo "")

if [[ -n "$response" ]]; then
    clean_response=$(echo "$response" | extract_json)
    SID=$(echo "$clean_response" | jq -r '.id // empty' 2>/dev/null || echo "")
    
    if [[ -n "$SID" ]]; then
        echo "  ✓ Authentication successful!"
    else
        [[ "$DEBUG" == "1" ]] && echo "  No session ID in: $clean_response"
    fi
fi

# If first method failed, try alternatives
if [[ -z "$SID" ]]; then
    echo "  First method failed, trying alternatives..."
    
    # Try with application/json content type
    response=$(curl -sS "${CURL_T[@]}" -c "$JAR" -b "$JAR" "${HC[@]}" \
        -H 'content-type: application/json' \
        -H 'x-requested-with: XMLHttpRequest' \
        -H "X-Token: $T:$TYPE_SFX" \
        -X POST "$BASE/api/auth/authentication" \
        --data "$auth_payload" 2>/dev/null || echo "")
    
    if [[ -n "$response" ]]; then
        clean_response=$(echo "$response" | extract_json)
        SID=$(echo "$clean_response" | jq -r '.id // empty' 2>/dev/null || echo "")
    fi
fi

if [[ -z "$SID" ]]; then
    echo "✗ Authentication failed"
    echo "Possible issues:"
    echo "  - Incorrect password"
    echo "  - 2FA required (set GLOBALEAKS_AUTHCODE)"
    echo "  - Server configuration changed"
    exit 1
fi

echo "✓ Session ID: ${SID:0:16}..."

# Finalize session
echo "→ 5) Finalizing session..."
finalized=0

for sfx in 180 299 733 711; do
    code=$(curl -sS "${CURL_T[@]}" -o /dev/null -w '%{http_code}' \
        -b "$JAR" "${HC[@]}" \
        -H 'content-type: application/json' \
        -H "X-Session: $SID" \
        -X POST "$BASE/api/auth/session" \
        --data "{\"role\":\"receiver\",\"token\":\"$T:$sfx\"}" 2>/dev/null || echo "000")
    
    echo "  Suffix :$sfx → HTTP $code"
    
    if [[ "$code" =~ ^20[0-9]$ ]]; then
        finalized=1
        break
    fi
done

if [[ $finalized -eq 0 ]]; then
    echo "✗ Session finalization failed, but session may still work"
fi

# Test session
echo "→ 6) Testing session..."
if tips_response=$(curl -fsS "${CURL_T[@]}" -H "X-Session: $SID" "$BASE/api/recipient/rtips" 2>/dev/null); then
    tips_count=$(echo "$tips_response" | jq length 2>/dev/null || echo "unknown")
    echo "✓ Session works! Retrieved $tips_count tips"
    echo
    echo "🎉 SUCCESS!"
    echo "Session ID: $SID"
    echo
    echo "To use this session in your Python app:"
    echo "export GLOBALEAKS_SESSION_ID='$SID'"
else
    echo "✗ Session test failed, but you can try using it anyway:"
    echo "export GLOBALEAKS_SESSION_ID='$SID'"
fi

# Cleanup
rm -f "$JAR" 2>/dev/null || true