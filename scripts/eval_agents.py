#!/usr/bin/env python3
"""
Evaluate agent reports against the benchmark using ScaBenchScorerV2.

Usage:
    # Evaluate all agents in reports/
    python scripts/eval_agents.py

    # Evaluate a specific agent
    python scripts/eval_agents.py --agents agent317

    # Evaluate specific projects only
    python scripts/eval_agents.py --projects code4rena_secondswap_2025_02

    # Use a different inference API
    python scripts/eval_agents.py --inference-api http://localhost:8087

    # Resume (skip projects already scored)
    python scripts/eval_agents.py --resume

    # Dry run
    python scripts/eval_agents.py --dry-run
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Add repo root to path so we can import validator modules
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from validator.scorer import ScaBenchScorerV2

BENCHMARK_FILE = "validator/curated-highs-only-2025-08-08.json"
DEFAULT_REPORTS_DIR = "reports"
DEFAULT_SCORING_OUTPUT = "scoring_output"
DEFAULT_INFERENCE_API = "http://localhost:8087"
EVAL_MAX_VULNS = 100


def load_benchmark():
    with open(BENCHMARK_FILE, "r") as f:
        data = json.load(f)
    return {
        entry["project_id"]: entry.get("vulnerabilities", [])
        for entry in data
        if entry.get("project_id") and entry.get("vulnerabilities")
    }


def load_agent_findings(report_file):
    """Extract vulnerabilities from an agent report file."""
    with open(report_file, "r") as f:
        data = json.load(f)

    if not data.get("success", False):
        return None, data.get("error", "Unknown error")

    try:
        vulns = data.get("report", {}).get("vulnerabilities", [])
    except AttributeError:
        return None, f"Invalid report format: {type(data.get('report'))}"

    return vulns[:EVAL_MAX_VULNS], None


def main():
    parser = argparse.ArgumentParser(description="Evaluate agent reports against benchmark")
    parser.add_argument(
        "--reports-dir", default=DEFAULT_REPORTS_DIR,
        help=f"Directory containing agent subdirs (default: {DEFAULT_REPORTS_DIR})",
    )
    parser.add_argument(
        "--output", default=DEFAULT_SCORING_OUTPUT,
        help=f"Output directory for scoring results (default: {DEFAULT_SCORING_OUTPUT})",
    )
    parser.add_argument(
        "--inference-api", default=DEFAULT_INFERENCE_API,
        help=f"Inference API URL (default: {DEFAULT_INFERENCE_API})",
    )
    parser.add_argument("--agents", nargs="+", help="Specific agent dirs to evaluate (default: all)")
    parser.add_argument("--projects", nargs="+", help="Specific project keys to evaluate (default: all)")
    parser.add_argument("--model", default="deepseek-ai/DeepSeek-V3-0324", help="Scorer LLM model")
    parser.add_argument("--confidence-threshold", type=float, default=0.75)
    parser.add_argument("--strict-matching", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Skip projects already scored")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api_key = os.getenv("CHUTES_API_KEY")
    if not api_key:
        print("ERROR: CHUTES_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    benchmark = load_benchmark()
    print(f"Benchmark: {len(benchmark)} projects loaded")

    # Discover agent directories
    reports_dir = Path(args.reports_dir)
    if args.agents:
        agent_dirs = [reports_dir / a for a in args.agents]
    else:
        agent_dirs = sorted(
            [d for d in reports_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
        )

    if not agent_dirs:
        print(f"No agent directories found in {reports_dir}")
        sys.exit(1)

    print(f"Agents: {[d.name for d in agent_dirs]}")
    print(f"Inference API: {args.inference_api}")
    print(f"Scorer model: {args.model}")
    print()

    all_summaries = {}

    for agent_dir in agent_dirs:
        agent_name = agent_dir.name
        agent_output_dir = Path(args.output) / agent_name
        os.makedirs(agent_output_dir, exist_ok=True)

        report_files = sorted(agent_dir.glob("*.json"))
        if not report_files:
            print(f"[{agent_name}] No report files found, skipping")
            continue

        # Filter to requested projects
        if args.projects:
            report_files = [f for f in report_files if f.stem in args.projects]

        # Filter to projects that have benchmark data
        scoreable = []
        no_benchmark = []
        for rf in report_files:
            project_key = rf.stem
            if project_key in benchmark:
                scoreable.append((project_key, rf))
            else:
                no_benchmark.append(project_key)

        # Resume support
        to_score = []
        already_done = []
        for project_key, rf in scoreable:
            score_file = agent_output_dir / f"{project_key}.json"
            if args.resume and score_file.exists():
                already_done.append(project_key)
            else:
                to_score.append((project_key, rf))

        print(f"[{agent_name}] {len(report_files)} reports, {len(to_score)} to score, "
              f"{len(already_done)} done, {len(no_benchmark)} no benchmark data")

        if no_benchmark:
            print(f"  No benchmark: {', '.join(no_benchmark)}")

        if not to_score:
            print(f"  Nothing to score")
            # Still load existing results for summary
            for project_key in already_done:
                score_file = agent_output_dir / f"{project_key}.json"
                try:
                    with open(score_file) as f:
                        all_summaries.setdefault(agent_name, []).append(json.load(f))
                except (json.JSONDecodeError, OSError):
                    pass
            continue

        if args.dry_run:
            print(f"  Would score: {[k for k, _ in to_score]}")
            continue

        # Initialize scorer for this agent
        scorer = ScaBenchScorerV2({
            "api_key": api_key,
            "api_url": args.inference_api,
            "model": args.model,
            "debug": args.debug,
            "verbose": args.verbose,
            "confidence_threshold": args.confidence_threshold,
            "strict_matching": args.strict_matching,
        })

        agent_results = []

        # Load existing results for summary
        for project_key in already_done:
            score_file = agent_output_dir / f"{project_key}.json"
            try:
                with open(score_file) as f:
                    agent_results.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass

        for i, (project_key, report_file) in enumerate(to_score, 1):
            print(f"\n  [{i}/{len(to_score)}] Scoring {project_key}...")
            t0 = time.time()

            findings, error = load_agent_findings(report_file)
            if findings is None:
                print(f"    SKIP: {error}")
                result_data = {
                    "project": project_key,
                    "agent": agent_name,
                    "status": "error",
                    "error": error,
                }
                score_file = agent_output_dir / f"{project_key}.json"
                with open(score_file, "w") as f:
                    json.dump(result_data, f, indent=2)
                agent_results.append(result_data)
                continue

            expected = benchmark[project_key]
            print(f"    {len(expected)} expected vs {len(findings)} found")

            # Reset token counters per project
            scorer.input_tokens = 0
            scorer.output_tokens = 0
            scorer.cached_tokens = 0

            result = scorer.score_project(
                expected_findings=expected,
                tool_findings=findings,
                project_name=project_key,
            )

            elapsed = time.time() - t0
            result_data = {
                "project": result.project,
                "agent": agent_name,
                "status": "success",
                "timestamp": result.timestamp,
                "elapsed_seconds": round(elapsed, 1),
                "total_expected": result.total_expected,
                "total_found": result.total_found,
                "true_positives": result.true_positives,
                "false_negatives": result.false_negatives,
                "false_positives": result.false_positives,
                "detection_rate": result.detection_rate,
                "precision": result.precision,
                "f1_score": result.f1_score,
                "matched_findings": result.matched_findings,
                "missed_findings": result.missed_findings,
                "undecided_findings": result.undecided_findings,
                "extra_findings": result.extra_findings,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cached_tokens": result.cached_tokens,
            }

            score_file = agent_output_dir / f"{project_key}.json"
            with open(score_file, "w") as f:
                json.dump(result_data, f, indent=2)

            agent_results.append(result_data)
            detection_pct = round(result.detection_rate * 100)
            print(f"    Detection: {detection_pct}% | TP: {result.true_positives}/{result.total_expected} | "
                  f"Precision: {result.precision:.1%} | F1: {result.f1_score:.1%} | {elapsed:.0f}s")

        all_summaries[agent_name] = agent_results

    if args.dry_run:
        return

    # Print comparative summary
    print(f"\n{'='*80}")
    print("EVALUATION SUMMARY")
    print(f"{'='*80}")

    for agent_name, results in sorted(all_summaries.items()):
        scored = [r for r in results if r.get("status") == "success"]
        if not scored:
            print(f"\n{agent_name}: no scored results")
            continue

        total_tp = sum(r["true_positives"] for r in scored)
        total_expected = sum(r["total_expected"] for r in scored)
        total_found = sum(r["total_found"] for r in scored)
        total_fp = sum(r["false_positives"] for r in scored)
        avg_detection = sum(r["detection_rate"] for r in scored) / len(scored)
        avg_precision = sum(r["precision"] for r in scored) / len(scored)
        avg_f1 = sum(r["f1_score"] for r in scored) / len(scored)

        print(f"\n{agent_name} ({len(scored)} projects scored):")
        print(f"  Avg Detection Rate: {avg_detection:.1%}")
        print(f"  Avg Precision:      {avg_precision:.1%}")
        print(f"  Avg F1:             {avg_f1:.1%}")
        print(f"  Total TP/Expected:  {total_tp}/{total_expected}")
        print(f"  Total Found:        {total_found} ({total_fp} FP)")

    # Save aggregate summary
    summary_file = Path(args.output) / "summary.json"
    summary = {}
    for agent_name, results in all_summaries.items():
        scored = [r for r in results if r.get("status") == "success"]
        if not scored:
            continue
        summary[agent_name] = {
            "projects_scored": len(scored),
            "avg_detection_rate": sum(r["detection_rate"] for r in scored) / len(scored),
            "avg_precision": sum(r["precision"] for r in scored) / len(scored),
            "avg_f1": sum(r["f1_score"] for r in scored) / len(scored),
            "total_true_positives": sum(r["true_positives"] for r in scored),
            "total_expected": sum(r["total_expected"] for r in scored),
            "total_found": sum(r["total_found"] for r in scored),
            "total_false_positives": sum(r["false_positives"] for r in scored),
            "per_project": {
                r["project"]: {
                    "detection_rate": r["detection_rate"],
                    "precision": r["precision"],
                    "f1_score": r["f1_score"],
                    "true_positives": r["true_positives"],
                    "total_expected": r["total_expected"],
                    "total_found": r["total_found"],
                }
                for r in scored
            },
        }

    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSummary saved to {summary_file}")


if __name__ == "__main__":
    main()
