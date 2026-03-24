#!/usr/bin/env bash
# Inference proxy contract tests.
# Verifies OpenAI-shaped responses, tool pass-through, truncation guard,
# default/override behaviour, multi-turn tools, and reasoning model fields.
#
# Requires the proxy to be running locally (default: http://localhost:8000).
#
# Usage:
#   ./scripts/test_inference_proxy.sh [PROXY_URL]
#
set -euo pipefail

PROXY="${1:-http://localhost:8000}"
TOOL_MODEL="${TOOL_MODEL:-deepseek-ai/DeepSeek-R1-0528-TEE}"
REASONING_MODEL="${REASONING_MODEL:-deepseek-ai/DeepSeek-R1-0528-TEE}"
PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

check() {
  local label="$1" ok="$2"
  if [ "$ok" = "true" ]; then
    green "  PASS: $label"
    PASS=$((PASS + 1))
  else
    red "  FAIL: $label"
    FAIL=$((FAIL + 1))
  fi
}

# ────────────────────────────────────────────────────────────────
bold "1: Default request → JSON mode, OpenAI response shape"
RESP=$(curl -sf "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-1' \
  -H 'X-Project-Id: test-proj' \
  -d '{
    "model": null,
    "messages": [
      {"role": "system", "content": "Reply with a JSON object: {\"ok\": true}"},
      {"role": "user",   "content": "ping"}
    ],
    "max_tokens": 64
  }')

echo "$RESP" | python3 -m json.tool

CHECKS=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = {}
c['has_choices']             = 'choices' in d and len(d['choices']) > 0
c['has_model']               = bool(d.get('model'))
c['has_id']                  = bool(d.get('id'))
c['has_usage']               = 'usage' in d
c['has_prompt_tokens']       = 'prompt_tokens' in d.get('usage', {})
c['has_completion_tokens']   = 'completion_tokens' in d.get('usage', {})
c['has_message']             = 'message' in d.get('choices', [{}])[0]
c['has_content']             = bool(d['choices'][0]['message'].get('content'))
c['role_is_assistant']       = d['choices'][0]['message'].get('role') == 'assistant'
c['finish_reason_is_stop']   = d['choices'][0].get('finish_reason') == 'stop'
for k, v in c.items():
    print(f'{k}={str(v).lower()}')
")

while IFS='=' read -r key val; do
  check "$key" "$val"
done <<< "$CHECKS"

echo

# ────────────────────────────────────────────────────────────────
bold "2: JSON mode + max_tokens=1 → 502 (truncation guard)"
HTTP_CODE=$(curl -s -o /tmp/test_proxy_2.json -w '%{http_code}' "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-2' \
  -H 'X-Project-Id: test-proj' \
  -d '{
    "model": null,
    "messages": [
      {"role": "system", "content": "Reply with a JSON object: {\"ok\": true}"},
      {"role": "user",   "content": "ping"}
    ],
    "max_tokens": 1
  }')

echo "HTTP $HTTP_CODE"
python3 -m json.tool /tmp/test_proxy_2.json 2>/dev/null || cat /tmp/test_proxy_2.json

check "truncation_returns_502" "$([ "$HTTP_CODE" = "502" ] && echo true || echo false)"

DETAIL=$(python3 -c "import json; d=json.load(open('/tmp/test_proxy_2.json')); print(d.get('detail',''))" 2>/dev/null || echo "")
check "detail_mentions_finish_reason_length" "$(echo "$DETAIL" | grep -q 'finish_reason=length' && echo true || echo false)"

echo

# ────────────────────────────────────────────────────────────────
bold "3: response_format=text + max_tokens=1 → 200 with finish_reason=length"
HTTP_CODE2=$(curl -s -o /tmp/test_proxy_3.json -w '%{http_code}' "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-3' \
  -H 'X-Project-Id: test-proj' \
  -d '{
    "model": null,
    "messages": [
      {"role": "system", "content": "Say hello"},
      {"role": "user",   "content": "hi"}
    ],
    "max_tokens": 1,
    "response_format": {"type": "text"}
  }')

echo "HTTP $HTTP_CODE2"
python3 -m json.tool /tmp/test_proxy_3.json 2>/dev/null || cat /tmp/test_proxy_3.json

check "text_mode_returns_200" "$([ "$HTTP_CODE2" = "200" ] && echo true || echo false)"

FR3=$(python3 -c "import json; d=json.load(open('/tmp/test_proxy_3.json')); print(d['choices'][0].get('finish_reason',''))" 2>/dev/null || echo "")
check "finish_reason_is_length" "$([ "$FR3" = "length" ] && echo true || echo false)"

echo

# ────────────────────────────────────────────────────────────────
bold "4: Defaults applied and explicit values override (check proxy logs)"
echo "  Sending minimal request (model + messages only)..."
curl -sf "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-4-minimal' \
  -H 'X-Project-Id: test-proj' \
  -d '{
    "model": null,
    "messages": [
      {"role": "system", "content": "Reply with JSON: {\"ok\": true}"},
      {"role": "user",   "content": "ping"}
    ]
  }' | python3 -m json.tool
echo "  → Check proxy logs for job_id=test-4-minimal:"
echo "    - Payload SHOULD have 'temperature': 0.2 (model default)"
echo "    - Payload SHOULD have 'max_tokens': 4096 (model default)"
echo "    - Payload SHOULD have 'response_format': {\"type\": \"json_object\"} (injected default)"

echo
echo "  Sending request with explicit temperature + max_tokens..."
curl -sf "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-4-explicit' \
  -H 'X-Project-Id: test-proj' \
  -d '{
    "model": null,
    "messages": [
      {"role": "system", "content": "Reply with JSON: {\"ok\": true}"},
      {"role": "user",   "content": "ping"}
    ],
    "temperature": 0.5,
    "max_tokens": 128
  }' | python3 -m json.tool
echo "  → Check proxy logs for job_id=test-4-explicit:"
echo "    - Payload SHOULD have 'temperature': 0.5 (caller override)"
echo "    - Payload SHOULD have 'max_tokens': 128 (caller override)"

echo

# ────────────────────────────────────────────────────────────────
bold "5: Tool use pass-through → tools accepted, response has OpenAI structure"
RESP5=$(curl -sf "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-5' \
  -H 'X-Project-Id: test-proj' \
  -d '{
    "model": null,
    "messages": [
      {"role": "user", "content": "What is 2+2? You must use the calculator tool to answer."}
    ],
    "tools": [{"type": "function", "function": {"name": "calculator", "description": "Calculate math expressions", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}}],
    "tool_choice": "auto",
    "max_tokens": 256,
    "response_format": {"type": "text"}
  }')

echo "$RESP5" | python3 -m json.tool

CHECKS5=$(echo "$RESP5" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = {}
c['tools_has_choices'] = 'choices' in d and len(d['choices']) > 0
c['tools_has_usage']   = 'usage' in d
msg = d['choices'][0].get('message', {})
c['tools_has_message'] = 'content' in msg or 'tool_calls' in msg
for k, v in c.items():
    print(f'{k}={str(v).lower()}')
")

while IFS='=' read -r key val; do
  check "$key" "$val"
done <<< "$CHECKS5"

echo

# ────────────────────────────────────────────────────────────────
bold "6: Multi-turn tool conversation → tool role messages accepted"
echo "  Using tool-capable model: $TOOL_MODEL"
PAYLOAD6=$(cat <<EOF
{
  "model": "$TOOL_MODEL",
  "messages": [
    {"role": "user", "content": "What is 2+2?"},
    {"role": "assistant", "content": null, "tool_calls": [{"id": "call_abc123", "type": "function", "function": {"name": "calculator", "arguments": "{\"expression\": \"2+2\"}"}}]},
    {"role": "tool", "tool_call_id": "call_abc123", "content": "4"}
  ],
  "tools": [{"type": "function", "function": {"name": "calculator", "description": "Calculate math", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}}],
  "max_tokens": 256,
  "response_format": {"type": "text"}
}
EOF
)
RESP6=$(curl -sf "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-6' \
  -H 'X-Project-Id: test-proj' \
  -d "$PAYLOAD6")

echo "$RESP6" | python3 -m json.tool

CHECKS6=$(echo "$RESP6" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = {}
c['multiturn_has_choices'] = 'choices' in d and len(d['choices']) > 0
c['multiturn_has_content'] = bool(d['choices'][0]['message'].get('content'))
for k, v in c.items():
    print(f'{k}={str(v).lower()}')
")

while IFS='=' read -r key val; do
  check "$key" "$val"
done <<< "$CHECKS6"

echo

# ────────────────────────────────────────────────────────────────
bold "7: Reasoning model → reasoning_content and reasoning_tokens in response"
echo "  Using reasoning model: $REASONING_MODEL"
PAYLOAD7=$(cat <<EOF
{
  "model": "$REASONING_MODEL",
  "messages": [
    {"role": "user", "content": "What is 25 * 37? Think step by step and reply with JSON: {\"answer\": <number>}"}
  ],
  "max_tokens": 1024,
  "response_format": {"type": "text"}
}
EOF
)
RESP7=$(curl -sf "$PROXY/inference" \
  -H 'Content-Type: application/json' \
  -H 'X-Job-Id: test-7' \
  -H 'X-Project-Id: test-proj' \
  -d "$PAYLOAD7")

echo "$RESP7" | python3 -m json.tool

CHECKS7=$(echo "$RESP7" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = {}
c['reasoning_has_choices'] = 'choices' in d and len(d['choices']) > 0
c['reasoning_has_reasoning_content'] = bool(d['choices'][0]['message'].get('reasoning_content'))
usage = d.get('usage', {})
ctd = usage.get('completion_tokens_details') or {}
rt = ctd.get('reasoning_tokens', 0) or usage.get('reasoning_tokens', 0)
c['reasoning_tokens_gt_0'] = rt > 0
for k, v in c.items():
    print(f'{k}={str(v).lower()}')
")

while IFS='=' read -r key val; do
  check "$key" "$val"
done <<< "$CHECKS7"

echo

# ────────────────────────────────────────────────────────────────
bold "Summary"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
[ "$FAIL" -eq 0 ] && green "  All checks passed!" || red "  Some checks failed."
exit "$FAIL"
