# Parallelize eval_agents.py — Implementation Plan

## Overview

Parallelize the eval scoring pipeline (`scripts/eval_agents.py`) to run agent×project pairs concurrently using `ThreadPoolExecutor` with 8 workers. Currently the pipeline is fully sequential (~30 hrs for 3 models); this brings wall clock per model down to roughly `total_calls / 8 * avg_latency`.

## Current State

- `eval_agents.py` loops: agents → projects → `scorer.score_project()` (sequential)
- `scorer.py` `find_match_in_results()` makes 1 LLM call per chunk of findings, via `self.prompt()` which does `requests.post` to the proxy
- Proxy (`api.py`) has `INFER_CONCURRENCY = 8` semaphore — this is the hard ceiling
- Each project produces an independent `score_{project}.json` — no cross-project state

### Key Discoveries:
- `ScaBenchScorerV2` has mutable `input_tokens`/`output_tokens`/`cached_tokens` counters (`scorer.py:99-101`) — not thread-safe, solved by one scorer per thread
- `rich.Console()` is module-level (`scorer.py:37`) — concurrent prints will garble output
- `scorer.py:155` `requests.post` has no timeout — can hang forever if proxy is dead
- `scorer.py:640` `Progress(console=console)` uses module-level console — must use `self.console`
- Token counters are reset per-project in `eval_agents.py:234-236` and saved per-project in the output JSON — aggregation happens from files, not from scorer state

## Desired End State

Running `python scripts/eval_agents.py` scores all agent×project pairs concurrently (up to 8 at a time) instead of sequentially. All agent×project pairs are flattened into a single pool — not per-agent sequential batches. Output files and summary JSON are identical to the sequential version. Wall clock improves ~8x for a single model run.

### Verification:
- Same `score_{project}.json` files produced (content may vary due to LLM non-determinism, but structure identical)
- Summary JSON at `scoring_output/summary.json` has same schema
- `--resume` still works (skip projects with existing score files)
- `--dry-run` still lists what would be scored
- No garbled console output
- Partial JSON files cannot be left behind on kill (atomic writes)

## What We're NOT Doing

- Parallelizing across models (user runs separate invocations per model)
- Parallelizing chunks within `find_match_in_results` — proxy is already saturated by 8 agent×project threads; chunk parallelism adds complexity for marginal gain and wastes LLM calls on early-exit paths
- Parallelizing across expected findings within a project — `matched_tool_indices` shared state conflict
- Changing the proxy concurrency or chutes_client
- Making scorer.py thread-safe with locks — we use separate instances instead
- Adding `--workers` CLI arg — hardcoded to 8

## Implementation Approach

Two files changed: `eval_agents.py` and `scorer.py`.

**`eval_agents.py`**: Restructure `main()` into two phases:
1. **Discovery phase** (sequential, fast): iterate agents, discover reports, filter by `--projects` and `--resume`, build a flat list of work units across all agents
2. **Execution phase** (parallel): submit all work units to `ThreadPoolExecutor(max_workers=8)`, collect results via `as_completed`, bucket by agent for summary

**`scorer.py`**: Three changes:
1. Add `console` parameter to `ScaBenchScorerV2.__init__` (dependency injection, default `Console()` for backwards compat)
2. Replace all 16 `console.print(...)` inside class methods with `self.console.print(...)`, including `Progress(console=self.console)` at line 640
3. Add `timeout=360` to `requests.post` at line 155

Each worker thread creates its own `ScaBenchScorerV2` with `Console(file=StringIO())` — output goes to a buffer (discarded). The main thread prints one-line progress per completed project.

---

## Phase 1: scorer.py changes

### Overview
Make `ScaBenchScorerV2` thread-safe by injecting console and adding request timeout. These changes are backwards-compatible — standalone `scorer.py` usage via `main()` is unaffected.

### Changes Required:

#### 1.1 Console injection in `__init__`

**File**: `validator/scorer.py`
**Changes**: Add `console` parameter, store as `self.console`.

```python
class ScaBenchScorerV2:
    def __init__(self, config: Optional[Dict[str, Any]] = None, console: Optional[Console] = None):
        self.console = console or Console()
        # ... rest of __init__ unchanged
```

#### 1.2 Replace all `console.print` inside class with `self.console.print`

**File**: `validator/scorer.py`
**Changes**: 16 occurrences inside class methods. Full list:

| Method | Lines | Notes |
|--------|-------|-------|
| `prompt()` | 172, 180, 182, 187, 201 | Retry warnings, error messages, empty content warning |
| `find_match_in_results()` | 431, 454, 486 | Verbose LLM response, scoring errors |
| `score_project()` | 523, 549, 582, 619, 687, 725, 783 | Panel, checking status, match/miss, table |

Plus one non-print console reference:
| `score_project()` | 640 | `Progress(..., console=console)` → `Progress(..., console=self.console)` |

The 6 `console.print` calls in the module-level `main()` function (lines 853, 880, 892, 895, 922, 933, 935) remain unchanged — they use the module-level `console` and only run in standalone mode.

#### 1.3 Add timeout to `requests.post`

**File**: `validator/scorer.py`
**Changes**: Line 155, add `timeout=360`.

```python
resp = requests.post(
    f"{self.api_url}/inference",
    headers=headers,
    json=payload,
    timeout=360,
)
```

360s = proxy's 300s upstream timeout + 60s buffer for proxy queue wait and overhead.

### Success Criteria:

#### Automated Verification:
- [x] `python validator/scorer.py --help` still works (standalone entry point unchanged)
- [x] `grep -n "console\." validator/scorer.py` shows no bare `console.` inside class methods (all should be `self.console.`)
- [x] `grep "timeout" validator/scorer.py` shows timeout on requests.post

#### Manual Verification:
- [ ] Standalone `python validator/scorer.py --benchmark ... --results-dir ...` still prints to terminal (uses default Console)

---

## Phase 2: eval_agents.py parallelization

### Overview
Restructure `main()` into discovery + execution phases. Flatten all agent×project work units into a single `ThreadPoolExecutor(max_workers=8)`. Add atomic JSON writes.

### Changes Required:

#### 2.1 New function `_score_one_project()`

**File**: `scripts/eval_agents.py`
**Changes**: Add top-level function — the unit of work for the thread pool.

```python
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console

MAX_WORKERS = 8


def _score_one_project(
    project_key: str,
    report_file: Path,
    expected_vulns: list,
    agent_name: str,
    agent_output_dir: Path,
    scorer_config: dict,
) -> dict:
    """Score a single agent×project pair. Thread-safe: owns its own scorer instance."""
    t0 = time.time()

    findings, error = load_agent_findings(report_file)
    if findings is None:
        result_data = {
            "project": project_key,
            "agent": agent_name,
            "status": "error",
            "error": error,
        }
        _atomic_json_write(agent_output_dir / f"score_{project_key}.json", result_data)
        return result_data

    buf = StringIO()
    quiet_console = Console(file=buf)
    scorer = ScaBenchScorerV2(scorer_config, console=quiet_console)

    result = scorer.score_project(
        expected_findings=expected_vulns,
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

    _atomic_json_write(agent_output_dir / f"score_{project_key}.json", result_data)
    return result_data
```

#### 2.2 Atomic JSON write helper

**File**: `scripts/eval_agents.py`

```python
def _atomic_json_write(path: Path, data: dict):
    """Write JSON atomically via tmp+rename to prevent corrupt files on kill."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, path)
```

#### 2.3 Restructure `main()` — discovery phase

**File**: `scripts/eval_agents.py`
**Changes**: Replace the sequential agent→project loop with two phases. The discovery phase collects all work units and pre-existing results across all agents.

```python
# === DISCOVERY PHASE (sequential, fast) ===
all_work_units = []   # [(project_key, report_file, expected, agent_name, output_dir, config)]
all_preloaded = {}    # {agent_name: [result_data, ...]}  — already-scored results

for agent_dir in agent_dirs:
    agent_name = agent_dir.name
    agent_output_dir = Path(args.output) / agent_name
    os.makedirs(agent_output_dir, exist_ok=True)

    report_files = sorted(agent_dir.glob("*.json"))
    if not report_files:
        print(f"[{agent_name}] No report files found, skipping")
        continue

    if args.projects:
        report_files = [f for f in report_files if f.stem in args.projects]

    scoreable = []
    no_benchmark = []
    for rf in report_files:
        project_key = rf.stem
        if project_key in benchmark:
            scoreable.append((project_key, rf))
        else:
            no_benchmark.append(project_key)

    to_score = []
    already_done = []
    for project_key, rf in scoreable:
        score_file = agent_output_dir / f"score_{project_key}.json"
        if args.resume and score_file.exists():
            already_done.append(project_key)
        else:
            to_score.append((project_key, rf))

    print(f"[{agent_name}] {len(report_files)} reports, {len(to_score)} to score, "
          f"{len(already_done)} done, {len(no_benchmark)} no benchmark data")
    if no_benchmark:
        print(f"  No benchmark: {', '.join(no_benchmark)}")

    # Load pre-existing results for summary
    preloaded = []
    for project_key in already_done:
        score_file = agent_output_dir / f"score_{project_key}.json"
        try:
            with open(score_file) as f:
                preloaded.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    all_preloaded[agent_name] = preloaded

    # Build scorer config (same for all projects under this agent)
    scorer_config = {
        "api_key": api_key,
        "api_url": args.inference_api,
        "debug": args.debug,
        "verbose": args.verbose,
        "confidence_threshold": args.confidence_threshold,
        "strict_matching": args.strict_matching,
    }
    if args.model:
        scorer_config["model"] = args.model

    if args.dry_run:
        if to_score:
            print(f"  Would score: {[k for k, _ in to_score]}")
        continue

    for project_key, rf in to_score:
        all_work_units.append((
            project_key, rf, benchmark[project_key],
            agent_name, agent_output_dir, scorer_config,
        ))

if args.dry_run:
    return
```

#### 2.4 Restructure `main()` — execution phase

**File**: `scripts/eval_agents.py`
**Changes**: Single thread pool over all work units, results bucketed by agent.

```python
# === EXECUTION PHASE (parallel) ===
all_results = {}  # {agent_name: [result_data, ...]}

# Seed with preloaded results
for agent_name, preloaded in all_preloaded.items():
    all_results.setdefault(agent_name, []).extend(preloaded)

if all_work_units:
    print(f"\nSubmitting {len(all_work_units)} scoring jobs to {MAX_WORKERS} workers...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_score_one_project, *unit): (unit[3], unit[0])  # (agent_name, project_key)
            for unit in all_work_units
        }
        for future in as_completed(futures):
            agent_name, project_key = futures[future]
            try:
                result_data = future.result()
                all_results.setdefault(agent_name, []).append(result_data)
                if result_data.get("status") == "success":
                    detection_pct = round(result_data["detection_rate"] * 100)
                    print(f"  [done] {agent_name}/{project_key} | "
                          f"Detection: {detection_pct}% | "
                          f"TP: {result_data['true_positives']}/{result_data['total_expected']} | "
                          f"F1: {result_data['f1_score']:.1%} | "
                          f"{result_data['elapsed_seconds']}s")
                else:
                    print(f"  [skip] {agent_name}/{project_key}: "
                          f"{result_data.get('error', 'unknown')}")
            except Exception as e:
                print(f"  [FAIL] {agent_name}/{project_key}: {e}")
                all_results.setdefault(agent_name, []).append({
                    "project": project_key,
                    "agent": agent_name,
                    "status": "error",
                    "error": str(e),
                })
elif not any(all_preloaded.values()):
    print("Nothing to score")
    return

# Use all_results instead of all_summaries for the summary section
all_summaries = all_results
```

The existing summary printing code (lines 282-338) works unchanged — it iterates `all_summaries.items()`.

### Success Criteria:

#### Automated Verification:
- [x] `python scripts/eval_agents.py --dry-run` lists all projects grouped by agent
- [ ] `python scripts/eval_agents.py --resume` with existing results loads them and prints summary
- [ ] `python scripts/eval_agents.py --agents agent317 --projects code4rena_secondswap_2025_02` scores one project correctly
- [ ] Full run produces same output file structure: `scoring_output/{agent}/score_{project}.json` + `scoring_output/summary.json`
- [x] No `.tmp` files left in output directories after clean run
- [x] `grep -r "ThreadPoolExecutor" scripts/eval_agents.py` confirms parallel execution

#### Manual Verification:
- [ ] Console output is clean — no garbled/interleaved lines
- [ ] Wall clock for 8 agent×project pairs is significantly less than sequential
- [ ] `--resume` correctly skips already-scored projects when re-run
- [ ] Kill mid-run: no corrupt `score_*.json` files (only `.tmp` files may remain, harmless)
- [ ] Progress lines show `agent_name/project_key` for easy identification

---

## Summary of changes

| File | Change | Lines affected |
|---|---|---|
| `validator/scorer.py` | Add `console` param to `__init__`, `self.console.print` (16 occurrences), `Progress(console=self.console)`, `timeout=360` on `requests.post` | ~20 lines changed |
| `scripts/eval_agents.py` | Add `_score_one_project()`, `_atomic_json_write()`, restructure `main()` into discovery+execution phases with flattened `ThreadPoolExecutor` | ~80 lines new, ~70 lines replaced |

Total: 2 files, ~100 net lines changed. No new dependencies.

## References

- Research doc: `scoring_baseline/RESEARCH.md`
- Key source files:
  - `scripts/eval_agents.py` — main eval loop
  - `validator/scorer.py:71-206` — ScaBenchScorerV2 class, prompt(), token counters
  - `validator/scorer.py:305-511` — find_match_in_results (chunk iteration)
  - `validator/scorer.py:513-804` — score_project
  - `validator/proxy/api.py:43-57` — proxy semaphore (8 slots)
  - `validator/proxy/chutes_client.py:22-27` — TIMEOUT=300, MAX_RETRIES=5
