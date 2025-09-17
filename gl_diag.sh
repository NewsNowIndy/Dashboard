#!/usr/bin/env bash
# globaleaks_diagnostic.sh - Diagnose GlobaLeaks connection and header issues

set -euo pipefail

BASE="${GLOBALEAKS_BASE_URL:-https://tips.indyleaks.com}"
USER="${GLOBALEAKS_USERNAME:?set GLOBALEAKS_USERNAME}"
DEBUG="${GLOBALEAKS_DEBUG:-1}"

echo "=== GlobaLeaks Connection Diagnostic Tool ==="
echo "Base URL: $BASE"
echo "Username: $USER"
echo

# Test 1: Basic connectivity and server response
echo "1. Testing basic server response..."
echo "curl -I $BASE"
if curl -I --connect-timeout 10 --max-time 15 "$BASE" 2>&1; then
    echo "✓ Server responds to HEAD request"
else
    echo "✗ Server doesn't respond - check URL and connectivity"
    exit 1
fi
echo

# Test 2: Get a token
echo "2. Testing /api/auth/token endpoint..."
TOKEN_RESPONSE=$(curl -sS --fail-with-body \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -H "Origin: $BASE" \
    -H "Referer: $BASE/#/login" \
    -X POST "$BASE/api/auth/token" \
    --data '{}' 2>&1 || echo "FAILED")

if [[ "$TOKEN_RESPONSE" == "FAILED" ]]; then
    echo "✗ Failed to get token"
    exit 1
fi

TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.id // empty' 2>/dev/null || echo "")
if [[ -z "$TOKEN" ]]; then
    echo "✗ No token in response: $TOKEN_RESPONSE"
    exit 1
fi

echo "✓ Got token: ${TOKEN:0:8}..."
echo

# Test 3: Test different ways of sending the token
echo "3. Testing token delivery methods..."

test_token_method() {
    local method="$1"
    local curl_args=("$@")
    shift
    
    echo "Testing method: $method"
    
    local response
    response=$(curl -sS --fail-with-body \
        -w "HTTPSTATUS:%{http_code}" \
        "${curl_args[@]}" \
        -H "Accept: application/json" \
        -H "Origin: $BASE" \
        -H "Referer: $BASE/#/login" \
        -H "X-Requested-With: XMLHttpRequest" \
        -H "Content-Type: text/plain; charset=UTF-8" \
        -X POST "$BASE/api/auth/type" \
        --data "{\"username\":\"$USER\"}" 2>&1 || echo "HTTPSTATUS:FAILED")
    
    local body status
    body="${response%HTTPSTATUS:*}"
    status="${response##*HTTPSTATUS:}"
    
    # Fix parsing issue - extract just the JSON part
    local clean_body
    clean_body=$(echo "$body" | grep -o '{.*}' | tail -n1 2>/dev/null || echo "$body")
    
    if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
        local salt
        salt=$(echo "$clean_body" | jq -r '.salt // empty' 2>/dev/null || echo "")
        if [[ -n "$salt" ]]; then
            echo "  ✓ SUCCESS - Got salt (len: ${#salt})"
            return 0
        else
            echo "  ✗ HTTP $status but no salt in: $clean_body"
        fi
    else
        echo "  ✗ HTTP $status: $(echo "$clean_body" | head -c 200)"
    fi
    return 1
}

# Try different token delivery methods
methods_worked=0

# Method 1: X-Token header only
if test_token_method "X-Token header only" -H "X-Token: $TOKEN:711"; then
    ((methods_worked++))
fi
echo

# Method 2: Cookie only
if test_token_method "Cookie only" -H "Cookie: token=$TOKEN"; then
    ((methods_worked++))
fi
echo

# Method 3: Both X-Token and Cookie
if test_token_method "X-Token + Cookie" -H "X-Token: $TOKEN:711" -H "Cookie: token=$TOKEN"; then
    ((methods_worked++))
fi
echo

# Method 4: Token in request body
if test_token_method "Token in body" --data "{\"username\":\"$USER\",\"token\":\"$TOKEN\"}"; then
    ((methods_worked++))
fi
echo

if [[ $methods_worked -eq 0 ]]; then
    echo "✗ NONE of the token delivery methods worked!"
    echo "This suggests the reverse proxy is stripping ALL token-related headers and data."
    echo
    echo "DIAGNOSIS: The server infrastructure is blocking token authentication."
    echo
    echo "SOLUTIONS:"
    echo "1. Contact the server administrator about header stripping"
    echo "2. Try accessing via onion address if available"
    echo "3. Check if there's a different API endpoint or authentication method"
    echo
    echo "Technical details:"
    echo "- The server accepts the initial /auth/token request (no auth needed)"
    echo "- But /auth/type fails because it can't see the token"
    echo "- This points to proxy/WAF configuration issues"
else
    echo "✓ $methods_worked token delivery method(s) worked for /auth/type"
    echo "The authentication issue may be specific to /auth/authentication endpoint"
fi

# Test 4: Check if server strips specific headers
echo
echo "4. Testing header passthrough..."

# Create a simple test server echo if available, or use httpbin-style service
echo "Testing if X-Token headers are passed through..."

# Try to see what headers the server actually receives by testing with a debug endpoint
# Many GlobaLeaks servers have debug info or we can infer from error messages

TEST_RESPONSE=$(curl -sS --fail-with-body \
    -H "X-Token: $TOKEN:711" \
    -H "X-Test-Header: diagnostic-test" \
    -H "Cookie: token=$TOKEN; test=value" \
    -H "Accept: application/json" \
    -H "Origin: $BASE" \
    -H "Referer: $BASE/#/login" \
    -X POST "$BASE/api/auth/authentication" \
    --data '{"tid":0,"username":"invalid-user-for-test","password":"invalid"}' 2>&1 || echo "FAILED")

echo "Authentication endpoint response with test headers:"
echo "$TEST_RESPONSE" | head -c 500
echo
echo

# Test 5: Try different authentication endpoint approaches
echo "5. Testing direct session creation..."

# Some GlobaLeaks versions allow direct session creation
SESSION_RESPONSE=$(curl -sS --fail-with-body \
    -w "HTTPSTATUS:%{http_code}" \
    -H "Accept: application/json" \
    -H "Origin: $BASE" \
    -H "Referer: $BASE/#/login" \
    -H "Content-Type: application/json" \
    -H "X-Token: $TOKEN:711" \
    -X POST "$BASE/api/auth/session" \
    --data '{"role":"receiver","username":"'$USER'","password":"test"}' 2>&1 || echo "HTTPSTATUS:FAILED")

session_body="${SESSION_RESPONSE%HTTPSTATUS:*}"
session_status="${SESSION_RESPONSE##*HTTPSTATUS:}"

echo "Direct session creation attempt: HTTP $session_status"
echo "Response: $(echo "$session_body" | head -c 200)"
echo

# Summary and recommendations
echo "=== DIAGNOSTIC SUMMARY ==="
if [[ $methods_worked -eq 0 ]]; then
    echo "❌ PROBLEM IDENTIFIED: Token authentication is completely blocked"
    echo
    echo "ROOT CAUSE: The reverse proxy or WAF is stripping authentication tokens"
    echo "from requests to the GlobaLeaks API endpoints."
    echo
    echo "IMMEDIATE SOLUTIONS:"
    echo "1. Use onion address (bypasses reverse proxy):"
    echo "   USE_ONION=1 GLOBALEAKS_ONION=<onion-url> ./gl_login.sh"
    echo
    echo "2. Contact server admin to whitelist these headers on /api/auth/* paths:"
    echo "   - X-Token"
    echo "   - Cookie (especially 'token' cookie)"
    echo "   - X-Requested-With"
    echo
    echo "3. If using nginx, the config needs:"
    echo "   proxy_set_header Cookie \$http_cookie;"
    echo "   proxy_set_header X-Token \$http_x_token;"
    echo "   proxy_pass_request_headers on;"
    echo
    echo "LONG-TERM: This is a server infrastructure issue that needs to be"
    echo "fixed by the GlobaLeaks server administrator."
else
    echo "✅ Token delivery works for some endpoints"
    echo "The issue may be specific to the /auth/authentication endpoint"
    echo "or there may be rate limiting/blocking on authentication attempts."
    echo
    echo "Try running the main script with these findings..."
fi

echo
echo "=== END DIAGNOSTIC ==="