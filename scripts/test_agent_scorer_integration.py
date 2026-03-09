#!/usr/bin/env python3
"""
Integration tests: BaselineRunner (agent) and ScaBenchScorerV2 (scorer)
complete analysis and scoring through the inference proxy with non-zero
token counts.

Usage:
    python scripts/test_agent_scorer_integration.py [PROXY_URL]

Requires:
    - Proxy running (default: http://localhost:8000)
    - CHUTES_API_KEY env var set (scorer needs it)
    - A local project directory with at least one .sol file (or a temp one is created)
"""
import sys
import os
import json
from pathlib import Path

# Ensure project root is on path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))

PROXY = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

PROJECT_DIR = _root / "validator" / "projects" / "code4rena_lambowin_2025_02"
SINGLE_FILE = PROJECT_DIR / "src" / "LamboToken.sol"

PASS = 0
FAIL = 0


def green(msg):
    print(f"\033[32m  PASS: {msg}\033[0m")


def red(msg):
    print(f"\033[31m  FAIL: {msg}\033[0m")


def bold(msg):
    print(f"\033[1m{msg}\033[0m")


def check(label, ok):
    global PASS, FAIL
    if ok:
        green(label)
        PASS += 1
    else:
        red(label)
        FAIL += 1


# ── Sample data for scorer tests ────────────────────────────────

EXPECTED_FINDINGS = [
    {
        "finding_id": "2024-12-lambowin_H-01",
        "severity": "high",
        "title": "Loss of User Funds in VirtualToken cashIn Function Due to Incorrect Amount Minting",
        "description": (
            "In the VirtualToken contract, cashIn() uses msg.value instead of amount "
            "for minting tokens when dealing with ERC20 tokens. Users lose their deposited "
            "ERC20 tokens as they receive 0 virtual tokens in return."
        ),
        "type": "logic error",
    },
]

TOOL_FINDINGS = [
    {
        "title": "Missing Access Control on Initialize Function",
        "description": (
            "The initialize() function can be called by any address to set the token "
            "name and symbol and mint the total supply to the caller."
        ),
        "severity": "critical",
        "type": "access control",
        "file": "src/LamboToken.sol",
        "id": "d26f00d58f4b8e0c",
    },
    {
        "title": "Incorrect Use of msg.value in cashIn for ERC20 Tokens",
        "description": (
            "The cashIn function in VirtualToken.sol uses msg.value for minting instead "
            "of the amount parameter. For ERC20 tokens, msg.value is 0, so users receive "
            "0 virtual tokens despite transferring ERC20 tokens."
        ),
        "severity": "high",
        "type": "logic error",
        "file": "src/VirtualToken.sol",
        "id": "abc123def456",
    },
    {
        "title": "LamboFactory DoS via createPair Frontrunning",
        "description": (
            "An attacker can pre-compute the address of the next LamboToken clone and "
            "call UniswapV2Factory.createPair before the token is deployed, causing all "
            "subsequent createLaunchPad calls to revert."
        ),
        "severity": "high",
        "type": "denial of service",
        "file": "src/LamboFactory.sol",
        "id": "def789ghi012",
    },
]


# ── Agent tests ─────────────────────────────────────────────────

def test_agent():
    from miner.agent import BaselineRunner

    config = {"model": "unsloth/gemma-3-12b-it"}
    runner = BaselineRunner(config, inference_api=PROXY)

    # ── analyze_file ──
    bold("Agent: analyze_file through proxy")

    if SINGLE_FILE.exists():
        test_file = SINGLE_FILE
        relative_path = str(test_file.relative_to(PROJECT_DIR))
    else:
        sol_files = list((_root / "validator" / "projects").rglob("*.sol"))
        sol_files = [f for f in sol_files if "test" not in f.name.lower() and f.stat().st_size < 15000]
        if not sol_files:
            print("No .sol files found locally. Creating a minimal test file.")
            test_dir = _root / "tmp_test_project"
            test_dir.mkdir(exist_ok=True)
            test_file = test_dir / "Test.sol"
            test_file.write_text(
                '// SPDX-License-Identifier: MIT\n'
                'pragma solidity ^0.8.0;\n\n'
                'contract Vault {\n'
                '    mapping(address => uint256) public balances;\n\n'
                '    function deposit() external payable {\n'
                '        balances[msg.sender] += msg.value;\n'
                '    }\n\n'
                '    function withdraw(uint256 amount) external {\n'
                '        // Reentrancy vulnerability: state updated after external call\n'
                '        (bool ok, ) = msg.sender.call{value: amount}("");\n'
                '        require(ok, "Transfer failed");\n'
                '        balances[msg.sender] -= amount;\n'
                '    }\n'
                '}\n'
            )
            relative_path = "Test.sol"
        else:
            test_file = sol_files[0]
            relative_path = test_file.name

    content = test_file.read_text()
    print(f"  Analyzing: {relative_path} ({len(content)} bytes)")

    vulnerabilities, input_tokens, output_tokens = runner.analyze_file(relative_path, content)

    check("analyze_file returned vulnerabilities object", vulnerabilities is not None)
    check(f"input_tokens > 0 (got {input_tokens})", input_tokens > 0)
    check(f"output_tokens > 0 (got {output_tokens})", output_tokens > 0)
    check(
        f"found at least 1 vulnerability (got {len(vulnerabilities.vulnerabilities)})",
        len(vulnerabilities.vulnerabilities) >= 1,
    )

    if vulnerabilities.vulnerabilities:
        v = vulnerabilities.vulnerabilities[0]
        check("vulnerability has title", bool(v.title))
        check("vulnerability has severity", bool(v.severity))
        check("vulnerability has description", bool(v.description))
        check(f"reported_by_model set to '{v.reported_by_model}'", v.reported_by_model == config["model"])

    # ── analyze_project ──
    print()
    bold("Agent: analyze_project (single-file project)")

    if SINGLE_FILE.exists():
        result = runner.analyze_project(
            source_dir=PROJECT_DIR / "src",
            project_name="lambowin_test",
            file_patterns=["LamboToken.sol"],
        )
    else:
        test_dir = _root / "tmp_test_project"
        result = runner.analyze_project(
            source_dir=test_dir,
            project_name="temp_test",
            file_patterns=["*.sol"],
        )

    check(f"files_analyzed > 0 (got {result.files_analyzed})", result.files_analyzed > 0)
    check(f"token_usage.input_tokens > 0 (got {result.token_usage['input_tokens']})", result.token_usage["input_tokens"] > 0)
    check(f"token_usage.output_tokens > 0 (got {result.token_usage['output_tokens']})", result.token_usage["output_tokens"] > 0)
    check(f"total_vulnerabilities > 0 (got {result.total_vulnerabilities})", result.total_vulnerabilities > 0)

    # Cleanup temp files
    tmp_dir = _root / "tmp_test_project"
    if tmp_dir.exists():
        import shutil
        shutil.rmtree(tmp_dir)


# ── Scorer tests ────────────────────────────────────────────────

def test_scorer():
    from validator.scorer import ScaBenchScorerV2

    api_key = os.getenv("CHUTES_API_KEY")
    if not api_key:
        print("SKIP: CHUTES_API_KEY not set, skipping scorer tests")
        return

    scorer_config = {
        "api_key": api_key,
        "api_url": PROXY,
        "model": "unsloth/gemma-3-12b-it",
        "debug": True,
        "verbose": True,
        "confidence_threshold": 0.75,
        "strict_matching": False,
    }

    bold("Scorer: score_project with 1 expected vs 3 tool findings")

    scorer = ScaBenchScorerV2(scorer_config)

    check("scorer initialized", scorer is not None)
    check(f"scorer.input_tokens starts at 0 (got {scorer.input_tokens})", scorer.input_tokens == 0)

    result = scorer.score_project(
        expected_findings=EXPECTED_FINDINGS,
        tool_findings=TOOL_FINDINGS,
        project_name="lambowin_integration_test",
    )

    check(f"total_expected == 1 (got {result.total_expected})", result.total_expected == 1)
    check(f"total_found == 3 (got {result.total_found})", result.total_found == 3)
    check(f"input_tokens > 0 (got {result.input_tokens})", result.input_tokens > 0)
    check(f"output_tokens > 0 (got {result.output_tokens})", result.output_tokens > 0)
    check(f"true_positives >= 1 (got {result.true_positives})", result.true_positives >= 1)
    check(f"detection_rate > 0 (got {result.detection_rate:.2f})", result.detection_rate > 0)
    check(f"false_positives >= 1 (got {result.false_positives})", result.false_positives >= 1)

    if result.matched_findings:
        mf = result.matched_findings[0]
        check("matched_finding has 'expected' key", "expected" in mf)
        check("matched_finding has 'matched' key", "matched" in mf)
        check("matched_finding has 'confidence' key", "confidence" in mf)
        check("matched_finding has 'justification' key", "justification" in mf)

    # ── Token accumulation ──
    print()
    bold("Scorer: token accumulation across calls")

    tokens_before = scorer.input_tokens
    result2 = scorer.score_project(
        expected_findings=EXPECTED_FINDINGS,
        tool_findings=TOOL_FINDINGS[:1],
        project_name="lambowin_accum_test",
    )

    check(
        f"input_tokens increased after 2nd call ({tokens_before} -> {scorer.input_tokens})",
        scorer.input_tokens > tokens_before,
    )
    check(f"output_tokens > 0 after 2nd call (got {scorer.output_tokens})", scorer.output_tokens > 0)


# ── Main ────────────────────────────────────────────────────────

def main():
    test_agent()
    print()
    test_scorer()

    print()
    bold("Summary")
    print(f"  Passed: {PASS}")
    print(f"  Failed: {FAIL}")
    if FAIL == 0:
        green("All integration checks passed!")
    else:
        red("Some integration checks failed.")
    sys.exit(FAIL)


if __name__ == "__main__":
    main()
