#!/usr/bin/env python3
"""
Minimal diagnostic: test if Chutes multi-model rotation returns empty content.

Sends the same kind of prompt the scorer sends (json_object format, security
matching task) to each model individually AND to the multi-model string.
Reports which models return content vs null/empty.

Usage:
    python scripts/test_empty_content.py
    python scripts/test_empty_content.py --rounds 5   # repeat to catch intermittent failures
"""

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("PROXY_URL", "http://localhost:8087")

MODELS = [
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "moonshotai/Kimi-K2.5-TEE",
    "MiniMaxAI/MiniMax-M2.5-TEE",
]
MULTI_MODEL = f"{','.join(MODELS)}:throughput"

# Minimal version of what the scorer sends
TEST_PROMPT = """
You are a security expert tasked with finding if a specific vulnerability was detected.

EXPECTED VULNERABILITY:
Title: execute calls can be front-run
Description: The execute function is publicly callable, enabling any external address to invoke it if a valid signature is provided. Anyone can front-run any execute call.
Severity: high
Type: N/A

TOOL FINDINGS:

[FINDING 0]
Title: Missing Access Control on Critical Transfer Functions
Severity: critical
Type: N/A
Description: The transferFromNative functions lack proper access control.

[FINDING 1]
Title: Signature Authorization Not Bound to Execution Context
Severity: high
Type: N/A
Description: The signature verification does not bind to execution context, allowing front-running.

Answer with a JSON object:
{
    "found": true/false,
    "matching_index": null or index of matching finding,
    "confidence": 0.0-1.0,
    "reason": "brief explanation"
}

IMPORTANT: Begin your response with `{"found":`
"""


def test_model(model_id, api_key, label=None):
    """Send one scoring prompt to a specific model. Returns (model, status, detail)."""
    label = label or model_id
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "You are a precise vulnerability matcher. Be strict."},
            {"role": "user", "content": TEST_PROMPT},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"x-chutes-api-key": api_key}

    t0 = time.time()
    try:
        resp = requests.post(f"{API_URL}/inference", headers=headers, json=payload, timeout=120)
        elapsed = time.time() - t0
    except requests.RequestException as e:
        return label, "CONNECTION_ERROR", str(e), 0

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        return label, f"HTTP_{resp.status_code}", detail, elapsed

    try:
        data = resp.json()
    except Exception:
        return label, "INVALID_JSON", resp.text[:200], elapsed

    choices = data.get("choices", [])
    if not choices:
        return label, "NO_CHOICES", str(data)[:200], elapsed

    content = choices[0].get("message", {}).get("content")
    finish_reason = choices[0].get("finish_reason", "unknown")
    served_model = data.get("model", "unknown")

    if content is None:
        return label, "CONTENT_NULL", f"finish_reason={finish_reason}, served_model={served_model}", elapsed
    if not content.strip():
        return label, "CONTENT_EMPTY", f"finish_reason={finish_reason}, served_model={served_model}", elapsed

    # Try parsing as JSON
    try:
        parsed = json.loads(content)
        return label, "OK", f"found={parsed.get('found')}, confidence={parsed.get('confidence')}, served_model={served_model}", elapsed
    except json.JSONDecodeError:
        return label, "BAD_JSON", f"content={content[:100]!r}, served_model={served_model}", elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1, help="Number of rounds per model")
    parser.add_argument("--multi-only", action="store_true", help="Only test multi-model string")
    args = parser.parse_args()

    api_key = os.getenv("CHUTES_API_KEY")
    if not api_key:
        print("ERROR: CHUTES_API_KEY not set")
        sys.exit(1)

    print(f"API: {API_URL}")
    print(f"Rounds per model: {args.rounds}")
    print()

    models_to_test = [("multi-model", MULTI_MODEL)] if args.multi_only else []
    if not args.multi_only:
        for m in MODELS:
            models_to_test.append((m.split("/")[-1], m))
        models_to_test.append(("multi-model", MULTI_MODEL))

    results = []
    for label, model_id in models_to_test:
        for r in range(1, args.rounds + 1):
            round_label = f"{label}" if args.rounds == 1 else f"{label} (round {r})"
            print(f"Testing {round_label}...", end=" ", flush=True)
            label_out, status, detail, elapsed = test_model(model_id, api_key, round_label)
            emoji = "OK" if status == "OK" else "FAIL"
            print(f"[{emoji}] {status} ({elapsed:.1f}s) — {detail}")
            results.append((label_out, status, detail))

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    ok = sum(1 for _, s, _ in results if s == "OK")
    fail = len(results) - ok
    print(f"  OK: {ok}/{len(results)}, FAIL: {fail}/{len(results)}")
    if fail:
        print("\n  Failures:")
        for label, status, detail in results:
            if status != "OK":
                print(f"    {label}: {status} — {detail}")


if __name__ == "__main__":
    main()
