#!/usr/bin/env python3
"""
Score external reports using the same evaluation pipeline as the validator.

Usage:
    # Score reports from the reports/ directory
    python scripts/score_reports.py

    # Print summary for an existing job run
    python scripts/score_reports.py --job-run-id 123

    # Print summary for an agent from the platform
    python scripts/score_reports.py --agent-id 456
"""

import argparse
import json
import os
import shutil
import time
from glob import glob
from pathlib import Path

import requests

from config import settings
from loggers.logger import get_logger
from validator.executor import AgentExecutor
from validator.manager import SandboxManager
from validator.models.platform import MockJobRun
from validator.platform_client import MockPlatformClient


logger = get_logger()

REPORTS_DIR = os.path.join(os.getcwd(), "reports")
JOBS_DIR = os.path.join(os.getcwd(), "jobs")
BENCHMARK_FILE = os.path.join(settings.validator_dir, "curated-highs-only-2025-08-08.json")


def load_benchmark_projects():
    """Load valid project keys from benchmark file."""
    with open(BENCHMARK_FILE, "r", encoding="utf-8") as f:
        benchmark_data = json.load(f)

    return {
        e["project_id"]
        for e in benchmark_data
        if e.get("project_id") and e.get("vulnerabilities")
    }


def extract_project_key(report_path: str) -> str | None:
    """Extract project key from report JSON data."""
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report_data = json.load(f)
        return report_data.get("report", {}).get("project")
    except Exception as e:
        logger.error(f"Failed to read project key from {report_path}: {e}")
        return None


def collect_reports_to_score():
    """Collect report files from reports/ directory."""
    reports = []
    valid_projects = load_benchmark_projects()

    if not os.path.exists(REPORTS_DIR):
        logger.error(f"Reports directory not found: {REPORTS_DIR}")
        return reports

    for json_file in glob(os.path.join(REPORTS_DIR, "*.json")):
        project_key = extract_project_key(json_file)
        if not project_key:
            logger.warning(f"Skipping {json_file}: could not extract project key")
            continue

        if project_key in valid_projects:
            reports.append((project_key, json_file))
        else:
            logger.warning(f"Skipping {json_file}: project_key '{project_key}' not in benchmark")

    return reports


def print_summary(results: list[dict], job_run_id: int | str):
    """Print a formatted summary of all scoring results."""
    print("\n" + "=" * 80)
    print(f"SCORING SUMMARY - Job Run {job_run_id}")
    print("=" * 80)

    successful = [r for r in results if r.get("status") == "success"]

    if not results:
        print("No results to display.")
        return

    total_expected = sum(r.get("result", {}).get("total_expected", 0) for r in successful)
    # total_found = sum(r.get("result", {}).get("total_found", 0) for r in successful)
    total_tp = sum(r.get("result", {}).get("true_positives", 0) for r in successful)
    # total_fn = sum(r.get("result", {}).get("false_negatives", 0) for r in successful)
    # total_fp = sum(r.get("result", {}).get("false_positives", 0) for r in successful)

    passed = sum(1 for r in successful if r.get("result", {}).get("result") == "PASS")
    failed = len(results) - passed

    overall_detection_rate = total_tp / total_expected if total_expected > 0 else 0
    overall_pass_rate = passed / len(results)
    # overall_precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0

    print(f"\n{'Project':<50} {'Detection':<12} {'TP/Expected':<15} {'Result':<8}")
    print("-" * 85)

    for r in sorted(results, key=lambda x: x.get("project", "")):
        project = r.get("project", "unknown")[:48]
        if r.get("status") == "success":
            result_data = r.get("result", {})
            detection = result_data.get("detection_rate", 0)
            tp = int(result_data.get("true_positives", 0))
            expected = int(result_data.get("total_expected", 0))
            final_result = result_data.get("result", "N/A")
            print(f"{project:<50} {detection*100:>6.1f}%      {tp:>3}/{expected:<3}          {final_result:<8}")
        else:
            error = r.get("error", "Unknown error")[:30]
            print(f"{project:<50} {'ERROR':<12} {error:<15}")

    print("\n" + "-" * 85)
    print(f"\n{'AGGREGATE STATISTICS':^85}")
    print("-" * 85)
    print(f"  Total Projects:         {len(results)}")
    print(f"  Successful Evaluations: {len(successful)}")
    print(f"  Failed Evaluations:     {len(results) - len(successful)}")
    print()
    print(f"  Passed:                 {passed}")
    print(f"  Failed:                 {failed}")
    print()
    print(f"  Total Expected:         {int(total_expected)}")
    # print(f"  Total Found:            {total_found}")
    print(f"  True Positives:         {int(total_tp)}")
    # print(f"  False Negatives:        {total_fn}")
    # print(f"  False Positives:        {total_fp}")
    print()
    print(f"  Overall Pass Rate:      {overall_pass_rate*100:.1f}%")
    print(f"  Overall Detection Rate: {overall_detection_rate*100:.1f}%")
    # print(f"  Overall Precision:      {overall_precision*100:.1f}%")
    print("=" * 80 + "\n")


def load_existing_evaluations(job_run_id: int | str) -> list[dict]:
    """Load evaluation.json files from an existing job run."""
    job_run_dir = os.path.join(JOBS_DIR, f"job_run_{job_run_id}", "reports")

    if not os.path.exists(job_run_dir):
        logger.error(f"Job run directory not found: {job_run_dir}")
        return []

    results = []
    for eval_file in glob(os.path.join(job_run_dir, "*", "evaluation.json")):
        try:
            with open(eval_file, "r", encoding="utf-8") as f:
                eval_data = json.load(f)

            project_key = Path(eval_file).parent.name
            results.append({
                "project": project_key,
                "status": eval_data.get("status", "unknown").lower().replace("status.", ""),
                "result": eval_data.get("result", {}),
                "error": eval_data.get("error"),
            })
        except Exception as e:
            logger.error(f"Failed to load {eval_file}: {e}")

    return results


def load_agent_evaluations(agent_id: int | str) -> list[dict]:
    """Load evaluation results from the platform API for a specific agent."""
    url = f"https://bitsec.ai/api/agents/{agent_id}/detail"

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        agent_data = response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch agent data: {e}")
        return []

    results = []
    executions = agent_data.get("executions", [])

    for execution in executions:
        evaluation = execution.get("evaluation") or {}
        detection_rate = evaluation.get("detection_rate", 0)
        true_positives = evaluation.get("true_positives", 0)
        total_expected = evaluation.get("total_expected", 0)

        results.append({
            "project": execution.get("project", "unknown"),
            "status": execution.get("status", "unknown").lower(),
            "error": execution.get("error"),
            "result": {
                "total_expected": total_expected,
                "true_positives": true_positives,
                "false_negatives": total_expected - true_positives,
                "detection_rate": detection_rate,
                "result": "PASS" if detection_rate == 1 else "FAIL",
            },
        })

    return results


def score_reports(reports: list[tuple[str, str]]) -> int:
    """Score reports and return the job run ID."""
    # Initialize SandboxManager (starts proxy)
    sandbox_manager = SandboxManager(is_local=True)

    # Setup job run
    job_run_id = int(time.time())
    job_run_reports_dir = os.path.join(JOBS_DIR, f"job_run_{job_run_id}", "reports")
    job_run = MockJobRun(id=job_run_id, job_id=0, validator_id=0)
    platform_client = MockPlatformClient()

    logger.info(f"Created job run {job_run_id}")

    for project_key, source_report in reports:
        executor = AgentExecutor(
            job_run=job_run,
            agent_filepath="",
            project_key=project_key,
            job_run_reports_dir=job_run_reports_dir,
            platform_client=platform_client,
        )

        # Copy report to expected location
        shutil.copy2(source_report, os.path.join(executor.project_report_dir, "report.json"))

        # Run evaluation
        executor.eval_job_run()

    return job_run_id


def main():
    parser = argparse.ArgumentParser(description="Score external reports or view existing job run or agent results")
    parser.add_argument(
        "--job-run-id",
        type=str,
        help="View summary for an existing job run instead of scoring new reports",
    )
    parser.add_argument(
        "--agent-id",
        type=str,
        help="View summary for an agent from the platform API",
    )

    args = parser.parse_args()

    if args.agent_id:
        results = load_agent_evaluations(args.agent_id)
        if results:
            print_summary(results, f"Agent {args.agent_id}")
        else:
            logger.error(f"No evaluations found for agent {args.agent_id}")

    elif args.job_run_id:
        results = load_existing_evaluations(args.job_run_id)
        if results:
            print_summary(results, args.job_run_id)
        else:
            logger.error(f"No evaluations found for job run {args.job_run_id}")

    else:
        reports = collect_reports_to_score()

        if not reports:
            logger.error("No valid reports found in reports/ directory")
            logger.info("Expected format: reports/*.json with report.project field matching benchmark")
            return

        logger.info(f"Found {len(reports)} reports to score:")
        for project_key, path in reports:
            logger.info(f"  - {project_key}: {path}")

        job_run_id = score_reports(reports)

        # Load results from saved evaluations
        results = load_existing_evaluations(job_run_id)
        print_summary(results, job_run_id)


if __name__ == "__main__":
    main()
