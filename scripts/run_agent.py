#!/usr/bin/env python3
"""
Run a miner agent across projects with resume support.
Skips projects that already have successful results.

Usage:
    # Run on all 31 benchmark projects
    python scripts/run_agent.py

    # Run on specific projects
    python scripts/run_agent.py --projects code4rena_secondswap_2025_02 code4rena_lambowin_2025_02

    # Use a different agent file
    python scripts/run_agent.py --agent miner/agent_v2.py

    # Use a different output directory
    python scripts/run_agent.py --output reports/agent2

    # Dry run (show what would be run)
    python scripts/run_agent.py --dry-run
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


BENCHMARK_FILE = "validator/curated-highs-only-2025-08-08.json"
PROJECTS_DIR = "projects"
DEFAULT_OUTPUT = "reports"
DEFAULT_AGENT = "miner/agent.py"
DEFAULT_INFERENCE_API = "http://localhost:8087"

PROJECTS_INDEX_FILE = "miner/projects.json"


def load_benchmark_project_ids():
    with open(BENCHMARK_FILE, "r") as f:
        data = json.load(f)
    return [e["project_id"] for e in data if e.get("project_id")]


def _load_projects_index():
    with open(PROJECTS_INDEX_FILE, "r", encoding="utf-8") as f:
        projects = json.load(f)
    return {p.get("project_key"): p for p in projects if p.get("project_key")}


def fetch_missing_projects(project_keys):
    """
    Fetch missing projects into projects/<project_key>/ using scripts/projects.py logic.
    Returns (fetched_keys, still_missing_keys).
    """
    try:
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        projects_py = Path(__file__).resolve().parent / "projects.py"
        spec = importlib.util.spec_from_file_location("projects_fetcher", str(projects_py))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        init_project = getattr(mod, "init_project")
    except Exception as e:
        print(f"\nERROR: could not load scripts/projects.py to fetch projects: {e}", flush=True)
        return [], list(project_keys)

    index = _load_projects_index()
    fetched = []
    still_missing = []
    for key in project_keys:
        project = index.get(key)
        if not project:
            still_missing.append(key)
            continue
        try:
            init_project(project)
            fetched.append(key)
        except Exception as e:
            print(f"  FAIL fetch {key}: {e}", flush=True)
            still_missing.append(key)
    return fetched, still_missing


def is_already_done(output_dir, project_key):
    """Check if a project already has a successful result."""
    result_file = os.path.join(output_dir, f"{project_key}.json")
    if not os.path.exists(result_file):
        return False

    try:
        with open(result_file, "r") as f:
            data = json.load(f)
        return data.get("success", False)
    except (json.JSONDecodeError, OSError):
        return False


def load_agent_module(agent_path):
    spec = importlib.util.spec_from_file_location("agent", agent_path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    if not hasattr(agent, "agent_main"):
        raise AttributeError(f"{agent_path} does not define agent_main()")

    patch_agent(agent)
    return agent


def _inference_call(inference_api, chutes_api_key, project_id, job_id, payload):
    """Shared inference HTTP call with API key header, heartbeat, and logging."""
    import requests as req
    import threading

    headers = {
        "x-chutes-api-key": chutes_api_key,
        "x-project-id": project_id or "local",
        "x-job-id": job_id,
    }
    t0 = time.time()
    done = threading.Event()

    def heartbeat():
        while not done.wait(60):
            elapsed = time.time() - t0
            print(f"    ... still waiting ({elapsed:.0f}s elapsed)", flush=True)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    try:
        resp = req.post(
            f"{inference_api}/inference",
            headers=headers,
            json=payload,
            timeout=300,
        )
    finally:
        done.set()
    elapsed = time.time() - t0
    if resp.status_code != 200:
        print(f"    inference HTTP {resp.status_code} ({elapsed:.1f}s)", flush=True)
    resp.raise_for_status()
    data = resp.json()
    in_tok = data.get("input_tokens", 0)
    out_tok = data.get("output_tokens", 0)
    print(f"    inference OK ({elapsed:.1f}s, {in_tok}+{out_tok} tokens)", flush=True)
    return data


def patch_agent(agent):
    """Patch agent module to fix missing API key header, file patterns, and silent errors."""
    chutes_api_key = os.getenv("CHUTES_API_KEY")
    if not chutes_api_key:
        raise RuntimeError("CHUTES_API_KEY not set. Add it to .env or export it.")

    # --- Patch inference methods to inject x-chutes-api-key header ---

    # agent-317: InferenceClient.call(self, messages)
    if hasattr(agent, "InferenceClient"):
        def patched_call(self, messages):
            payload = {"model": self.config["model"], "messages": messages}
            return _inference_call(
                self.inference_api, chutes_api_key, self.project_id, self.job_id, payload
            )
        agent.InferenceClient.call = patched_call

    # agent-794: AgenticFileRanker.inference(self, messages, model=...)
    if hasattr(agent, "AgenticFileRanker"):
        import inspect
        orig_sig = inspect.signature(agent.AgenticFileRanker.inference)
        ranker_default_model = orig_sig.parameters["model"].default

        def patched_ranker_inference(self, messages, model=ranker_default_model):
            payload = {"model": model, "messages": messages}
            return _inference_call(
                self.inference_api, chutes_api_key, self.project_id, self.job_id, payload
            )
        agent.AgenticFileRanker.inference = patched_ranker_inference

    # agent-794: BaselineRunner.inference(self, messages, model=None)
    if hasattr(agent, "BaselineRunner"):
        def patched_runner_inference(self, messages, model=None):
            payload = {
                "model": model or self.config["model"],
                "messages": messages,
                "temperature": 0.01,
            }
            return _inference_call(
                self.inference_api, chutes_api_key, self.project_id, self.job_id, payload
            )
        agent.BaselineRunner.inference = patched_runner_inference

    # --- Patch _get_file_patterns to include .cairo files ---
    for cls_name in ("ImprovedBaselineRunner", "BaselineRunner"):
        cls = getattr(agent, cls_name, None)
        if cls and hasattr(cls, "_get_file_patterns"):
            orig = cls._get_file_patterns

            def patched_get_file_patterns(self, file_patterns, _orig=orig):
                patterns = _orig(self, file_patterns)
                if "**/*.cairo" not in patterns:
                    patterns.append("**/*.cairo")
                return patterns

            cls._get_file_patterns = patched_get_file_patterns

    # --- Patch analyze_file to raise on errors instead of silently returning empty (agent-317) ---
    if hasattr(agent, "ImprovedBaselineRunner"):
        def patched_analyze_file(self, relative_path, content):
            print(f"    analyzing {relative_path} ({len(content)} bytes)...", flush=True)
            file_path = Path(relative_path)
            parser = agent.PydanticOutputParser(pydantic_object=agent.Vulnerabilities)
            format_instructions = parser.get_format_instructions()
            system_prompt = agent.PromptBuilder.build_system_prompt(format_instructions)
            user_prompt = agent.PromptBuilder.build_user_prompt(
                relative_path, content, file_path.suffix
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            response = self.inference_client.call(messages=messages)
            response_content = response["content"].strip()
            msg_json = agent.ResponseProcessor.clean_json_response(response_content)
            vulnerabilities = agent.Vulnerabilities(**msg_json)
            vulnerabilities = agent.ResponseProcessor.filter_vulnerabilities(
                vulnerabilities, self.config["model"]
            )
            n = len(vulnerabilities.vulnerabilities)
            print(f"    done {relative_path} — {n} vulnerabilities found", flush=True)
            input_tokens = response.get("input_tokens", 0)
            output_tokens = response.get("output_tokens", 0)
            return vulnerabilities, input_tokens, output_tokens

        agent.ImprovedBaselineRunner.analyze_file = patched_analyze_file


def run_one_project(agent_module, project_dir, project_key, output_dir, inference_api):
    """Run agent on a single project and save result. Returns True on success."""
    result_file = os.path.join(output_dir, f"{project_key}.json")
    start = time.time()

    try:
        result = agent_module.agent_main(project_dir, inference_api=inference_api)
        elapsed = time.time() - start

        # Detect silent failures
        report = result or {}
        tokens = report.get("token_usage", {}).get("total_tokens", 0)
        files_analyzed = report.get("files_analyzed", 0)
        files_skipped = report.get("files_skipped", 0)
        if files_analyzed == 0:
            raise RuntimeError(
                f"0 files analyzed ({files_skipped} skipped) — no files were successfully processed"
            )
        if tokens == 0:
            raise RuntimeError(
                f"Analyzed {files_analyzed} files but got 0 tokens — inference calls failed silently"
            )

        output = {
            "success": True,
            "report": result,
            "elapsed_seconds": round(elapsed, 1),
        }
        with open(result_file, "w") as f:
            json.dump(output, f, indent=2)

        print(f"  OK  {project_key} ({elapsed:.0f}s)")
        return True

    except SystemExit as e:
        elapsed = time.time() - start
        output = {
            "success": False,
            "error": f"agent_main called sys.exit({e.code})",
            "traceback": traceback.format_exc(),
            "elapsed_seconds": round(elapsed, 1),
        }
        with open(result_file, "w") as f:
            json.dump(output, f, indent=2)

        print(f"  FAIL {project_key}: agent_main called sys.exit({e.code})")
        return False

    except Exception as e:
        elapsed = time.time() - start
        output = {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "elapsed_seconds": round(elapsed, 1),
        }
        with open(result_file, "w") as f:
            json.dump(output, f, indent=2)

        print(f"  FAIL {project_key}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Run miner agent with resume support")
    parser.add_argument("--agent", default=DEFAULT_AGENT, help=f"Agent file (default: {DEFAULT_AGENT})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Output directory (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--inference-api", default=DEFAULT_INFERENCE_API, help=f"Inference API URL (default: {DEFAULT_INFERENCE_API})")
    parser.add_argument("--projects", nargs="+", help="Specific project keys to run (default: all 31)")
    parser.add_argument("--projects-dir", default=PROJECTS_DIR, help=f"Projects source dir (default: {PROJECTS_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="Show what would run without running")
    parser.add_argument("--force", action="store_true", help="Re-run even if result exists")
    parser.add_argument("--fetch-missing", action="store_true", help="If source is missing, fetch it from miner/projects.json into projects/<key>/ before running")
    args = parser.parse_args()

    # Resolve project list
    if args.projects:
        project_keys = args.projects
    else:
        project_keys = load_benchmark_project_ids()

    os.makedirs(args.output, exist_ok=True)

    # Classify projects
    to_run = []
    skipped = []
    missing = []

    for key in project_keys:
        project_dir = os.path.join(args.projects_dir, key)
        if not os.path.isdir(project_dir):
            missing.append(key)
            continue
        if not args.force and is_already_done(args.output, key):
            skipped.append(key)
            continue
        to_run.append((key, project_dir))

    # Summary
    print(f"Agent:    {args.agent}")
    print(f"Output:   {args.output}")
    print(f"API:      {args.inference_api}")
    print(f"Projects: {len(project_keys)} total, {len(to_run)} to run, {len(skipped)} done, {len(missing)} missing source")

    if missing:
        print(f"\nMissing source ({len(missing)}):")
        for key in missing:
            print(f"  - {key}")

        if args.fetch_missing and not args.dry_run:
            print("\nFetching missing projects...", flush=True)
            fetched, still_missing = fetch_missing_projects(missing)
            if fetched:
                print(f"Fetched ({len(fetched)}):")
                for key in fetched:
                    print(f"  - {key}")
            if still_missing:
                print(f"\nStill missing ({len(still_missing)}):")
                for key in still_missing:
                    print(f"  - {key}")

            # Re-classify after fetch
            to_run = []
            skipped = []
            missing = []
            for key in project_keys:
                project_dir = os.path.join(args.projects_dir, key)
                if not os.path.isdir(project_dir):
                    missing.append(key)
                    continue
                if not args.force and is_already_done(args.output, key):
                    skipped.append(key)
                    continue
                to_run.append((key, project_dir))

    if skipped:
        print(f"\nSkipping ({len(skipped)} already done):")
        for key in skipped:
            print(f"  - {key}")

    if not to_run:
        print("\nNothing to run.")
        return

    print(f"\nWill run ({len(to_run)}):")
    for key, _ in to_run:
        print(f"  - {key}")

    if args.dry_run:
        print("\n(dry run, exiting)")
        return

    # Load agent once
    print(f"\nLoading agent: {args.agent}")
    agent_module = load_agent_module(args.agent)

    # Run
    success = 0
    fail = 0
    total_start = time.time()

    for i, (key, project_dir) in enumerate(to_run, 1):
        print(f"\n[{i}/{len(to_run)}] {key}")
        if run_one_project(agent_module, project_dir, key, args.output, args.inference_api):
            success += 1
        else:
            fail += 1

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"Done: {success} ok, {fail} failed, {len(skipped)} skipped ({total_elapsed:.0f}s)")


if __name__ == "__main__":
    main()
