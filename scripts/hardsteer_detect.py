#!/usr/bin/env python3
"""Run hard-steering detection prompt against miner/agent.py via Claude Code."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from loggers.logger import get_logger
from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parent.parent
CURATED_PATH = REPO_ROOT / "validator" / "curated-highs-only-2025-08-08.json"
PROMPT_TEMPLATE_PATH = REPO_ROOT / "scripts" / "HARDSTEER_DETECT.md"
SUBMISSION_CODE_PATH = REPO_ROOT / "miner" / "agent.py"
logger = get_logger()
CLAUDE_MODEL = "claude-opus-4-6"
CLAUDE_TIMEOUT_SECONDS = 15 * 60

PROJECT_KEYS = [
    "code4rena_superposition_2025_01",
    "code4rena_lambowin_2025_02",
    "code4rena_bakerfi-invitational_2025_02",
    "cantina_minimal-delegation_2025_04",
    "code4rena_kinetiq_2025_07",
    "cantina_smart-contract-audit-of-tn-contracts_2025_08",
    "code4rena_forte-float128-solidity-library_2025_04",
    "sherlock_perennial_v2_update_3_2024_08",
    "sherlock_axion_2025_01",
    "sherlock_oku_2024_12",
]

# Optional: set to a file path to save the rendered prompt for inspection.
# Example: RENDERED_PROMPT_OUT = REPO_ROOT / "reports" / "hardsteer_prompt.txt"
RENDERED_PROMPT_OUT: Path | None = REPO_ROOT / "PROMPT.md"


class HardSteeringAssessment(BaseModel):
    """Result from hard-steering detection."""

    hard_steering_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    classification: str
    matched_solution_function_names: list[str] = Field(default_factory=list)
    summary: str
    evidence: list[str] = Field(default_factory=list)
    reasoning: str


HARD_STEERING_ASSESSMENT_SCHEMA = HardSteeringAssessment.model_json_schema()


def load_curated_entries(curated_path: Path) -> list[dict]:
    try:
        data = json.loads(curated_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Curated vulnerabilities file not found: {curated_path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in curated vulnerabilities file: {curated_path}\n{exc}") from exc

    if not isinstance(data, list):
        raise SystemExit(f"Expected a JSON array in curated vulnerabilities file: {curated_path}")
    return data


def filter_by_project_keys(entries: list[dict], requested_keys: list[str]) -> tuple[list[dict], list[str]]:
    requested = set(requested_keys)
    matched: list[dict] = [entry for entry in entries if str(entry.get("project_id", "")).strip() in requested]
    found_keys: set[str] = {str(entry.get("project_id", "")).strip() for entry in matched}

    missing = [key for key in requested_keys if key not in found_keys]
    return matched, missing


def build_prompt(prompt_template: str, submission_code: str, filtered_known_solutions: str) -> str:
    return prompt_template.format(
        SUBMISSION_CODE=submission_code,
        KNOWN_SOLUTION_LIST=filtered_known_solutions,
    )


def check_claude_ready() -> bool:
    """Quick preflight check to ensure Claude is reachable before long runs."""
    command = [
        "claude",
        "-p",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--model",
        CLAUDE_MODEL,
        "Reply with READY",
    ]
    logger.info("Running Claude readiness check.")
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.error("Claude readiness check failed: %s", exc)
        return False
    except subprocess.TimeoutExpired:
        logger.error("Claude readiness check timed out after 20s")
        return False

    combined = f"{result.stdout}\n{result.stderr}".lower()
    if "hit your limit" in combined:
        logger.error("Claude reports usage limit reached: %s", result.stdout)
        return False
    if result.returncode != 0:
        logger.error("Claude readiness check failed with status %s", result.returncode)
        return False

    logger.info("Claude readiness check passed.")
    return True


def run_assessment(prompt: str, json_schema: dict[str, Any]) -> dict[str, Any]:
    """Run a single Claude prompt and parse the JSON assessment response."""
    command = [
        "claude",
        "-p",
        "--no-session-persistence",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--model",
        CLAUDE_MODEL,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(json_schema),
    ]

    logger.info("Starting Claude prompt execution.")

    try:
        result = subprocess.run(
            command,
            input=prompt,
            check=False,
            text=True,
            capture_output=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise SystemExit("Could not find `claude` CLI in PATH. Install Claude Code CLI or update PATH.") from exc
    except OSError as exc:
        raise SystemExit(f"Failed to launch `claude -p`: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        logger.error("Claude timed out after %ss", exc.timeout)
        return {}

    if result.stderr:
        logger.warning("claude stderr:\n%s", result.stderr.rstrip())
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"claude exited with non-zero status: {result.returncode}: {detail}")
    if not result.stdout:
        raise SystemExit("claude returned empty stdout.")

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("Could not parse claude stdout as JSON.")
        logger.info("claude stdout (raw):\n%s", result.stdout.rstrip())
        return {}

    logger.info("claude stdout:\n%s", json.dumps(parsed, ensure_ascii=False, indent=2))
    return parsed


def main() -> int:
    if not check_claude_ready():
        return 1

    keys = [key.strip() for key in PROJECT_KEYS if key.strip()]
    if not keys:
        raise SystemExit("PROJECT_KEYS is empty. Set at least one project key in this file.")

    entries = load_curated_entries(CURATED_PATH)
    filtered_entries, missing_keys = filter_by_project_keys(entries, keys)
    if not filtered_entries:
        raise SystemExit("No curated projects matched the provided project keys: " + ", ".join(keys))

    if missing_keys:
        logger.warning(
            "These project keys were not found in curated highs: %s",
            ", ".join(missing_keys),
        )

    try:
        submission_code = SUBMISSION_CODE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemExit(f"Submission code file not found: {SUBMISSION_CODE_PATH}") from exc

    try:
        prompt_template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemExit(f"Prompt template file not found: {PROMPT_TEMPLATE_PATH}") from exc

    filtered_known_solutions = json.dumps(filtered_entries, ensure_ascii=False, indent=2)
    prompt = build_prompt(
        prompt_template=prompt_template,
        submission_code=submission_code,
        filtered_known_solutions=filtered_known_solutions,
    )

    if RENDERED_PROMPT_OUT is not None:
        RENDERED_PROMPT_OUT.parent.mkdir(parents=True, exist_ok=True)
        RENDERED_PROMPT_OUT.write_text(prompt, encoding="utf-8")
        logger.info("Rendered prompt written to: %s", RENDERED_PROMPT_OUT)

    logger.info(
        "Matched %s project(s) for %s requested key(s).",
        len(filtered_entries),
        len(keys),
    )

    assessment_response = run_assessment(
        prompt=prompt,
        json_schema=HARD_STEERING_ASSESSMENT_SCHEMA,
    )
    logger.info("ASSESSMENT_RESULT:\n%s", assessment_response.get("result"))
    structured_output = assessment_response.get("structured_output", {})
    logger.info("HARD_STEERING_SCORE: %s", structured_output.get("hard_steering_score"))
    return 0


if __name__ == "__main__":
    main()
