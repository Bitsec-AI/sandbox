---
date: 2026-04-05
git_commit: c52837a
branch: y/benchmark
repository: Bitsec-AI/sandbox
topic: "Parallel eval pipeline design for multi-model baseline comparison"
tags: [research, scoring, parallelism, eval_agents, benchmark]
status: complete
last_updated: 2026-04-05
---

# Research: Parallel Eval Pipeline Design

## Goal

Compare scorer models (Kimi-K2.5-TEE, GLM-5-TEE, MiniMax-M2.5-TEE) on the same
agent reports to establish whether they converge on the same evaluation scores.
The current sequential pipeline is too slow for iterative comparison.

## Selected Projects (4)

| Project | Expected Vulns | Why Selected |
|---------|---------------|--------------|
| `code4rena_secondswap_2025_02` | 3 | Kimi/GLM disagree (1 vs 3 TP) |
| `sherlock_oku_2024_12` | 8 | Kimi/GLM disagree (1 vs 4 TP) |
| `code4rena_bakerfi-invitational_2025_02` | 7 | Both agents find 2/7, good consistency test |
| `sherlock_cork-protocol_2025_01` | 11 | Big agent spread + Kimi/GLM disagree (0 vs 2) |

Agents: agent317, agent794

## Model Strings

| Model | String | Notes |
|-------|--------|-------|
| Kimi (no thinking) | `moonshotai/Kimi-K2.5-TEE` | Thinking disabled via scorer.py:141-143 |
| GLM | `zai-org/GLM-5-TEE` | From scripts/test_empty_content.py:28 |
| MiniMax | `MiniMaxAI/MiniMax-M2.5-TEE` | From scorer.py:67 (commented out) |

## Inference Call Estimate

Total calls per model: 426 (worst case, no early exit).
Total across 3 models: 1,278 calls.

Sequential estimates:
- Kimi (~10s/call): ~71 min per model
- GLM (~120s/call): ~852 min per model

## Parallelization Design

### Architecture

```
Main process (per model, 3 separate processes)
  └── ThreadPoolExecutor(max_workers=4)
        ├── Thread: agent317/secondswap  → own ScaBenchScorerV2 instance
        ├── Thread: agent317/oku         → own ScaBenchScorerV2 instance
        ├── Thread: agent794/bakerfi     → own ScaBenchScorerV2 instance
        └── Thread: agent794/cork        → own ScaBenchScorerV2 instance
              │
              ▼
        All threads POST to same proxy at localhost:8087
              │
              ▼
        Proxy semaphore (8 concurrent slots, FIFO queue)
              │
              ▼
        Chutes API (with exponential backoff on 429s)
```

### Design Decisions

1. **Buffer per-thread console output**: Each thread creates its own
   `rich.Console(file=StringIO())`. Main thread prints lightweight progress
   lines (started/finished) for visibility.

2. **One scorer instance per thread**: Eliminates token counter race conditions.
   No shared mutable state between threads.

3. **Add timeout to scorer `requests.post`**: `scorer.py:155` currently has no
   timeout. Add `timeout=360` (slightly above proxy's 300s timeout) to prevent
   indefinite hangs when proxy queue is saturated.

4. **Aggregate after thread join**: Each thread returns its result list. Main
   thread collects all results after `executor.shutdown()` and builds summary.

5. **Separate output dirs per model**: `scoring_baseline/kimi/`, `/glm/`,
   `/minimax/`. Eliminates file write conflicts and resume flag conflicts.

6. **Thread pool sizing**: `max_workers=4` per process. With 3 processes sharing
   8 proxy slots, each gets ~2-3 effective concurrent calls. 4 threads provides
   mild over-subscription to keep slots filled during backoff gaps.

7. **Error handling**: Fail-isolated. Each thread catches exceptions, records
   error in result JSON, continues. Other threads unaffected. Summary shows
   partial results.

### Thread Safety Analysis

| Concern | Severity | Resolution |
|---------|----------|------------|
| `rich.Console` interleaving | P1 cosmetic | Buffer per-thread, print summary after |
| Token counter `+=` races | P2 data | One scorer per thread (eliminated) |
| Scorer `requests.post` no timeout | P3 liveness | Add timeout=360 |
| Summary dict writes | P4 data | Aggregate after join |
| Proxy `requests.Session` sharing | P5 pre-existing | Not new; works in practice |

### Proxy Constraints

- **Single proxy instance** serves all 3 model processes. No separate proxies needed.
- **`INFER_CONCURRENCY = 8`** (`api.py:43`): Hard cap, asyncio semaphore, FIFO queue.
- Proxy runs default 1 uvicorn worker. Semaphore is the global ceiling.
- Requests exceeding 8 concurrent queue at `async with _sem:` (`api.py:55`).
- **`TIMEOUT = 300`** (`chutes_client.py:22`): Per-call timeout to Chutes API.
- **`MAX_RETRIES = 5`** with exponential backoff (1.5s, 3s, 6s, 12s) for 429/502.

### Other Notes

- **Temperature**: Defaults to 0.2 via `models.py:18`. Scorer doesn't set it.
  All models see the same temperature. Non-deterministic but consistent across
  models. Consider testing temperature=0 for reproducibility.

- **Early exit exists**: `scorer.py:472-481` returns immediately on confidence
  >= threshold. Concurrent chunk dispatch would waste calls for TPs but help
  for FNs. Not implementing concurrent chunks in v1.

- **`:throughput` suffix**: `CHUTES_MODELS` adds it; `--model` flag does not.
  For baseline comparison, consistency matters more than matching prod.

- **MiniMax untested**: First time through this pipeline. May surface new
  model-specific issues (null content, unexpected response format).

### Future Features (Flagged, Not Implementing)

- **Parallel expected findings within a project**: Requires `matched_tool_indices`
  conflict resolution (two expected findings matching same tool finding).
- **Prefilter limit**: Setting `prefilter_limit > 0` to reduce chunks. Potential
  accuracy side effects need validation.

## Commands to Run

```bash
# Run 3 models in parallel (3 separate terminal tabs)
python scripts/eval_agents.py \
  --agents agent317 agent794 \
  --projects code4rena_secondswap_2025_02 sherlock_oku_2024_12 \
           code4rena_bakerfi-invitational_2025_02 sherlock_cork-protocol_2025_01 \
  --model "moonshotai/Kimi-K2.5-TEE" \
  --output scoring_baseline/kimi

python scripts/eval_agents.py \
  --agents agent317 agent794 \
  --projects code4rena_secondswap_2025_02 sherlock_oku_2024_12 \
           code4rena_bakerfi-invitational_2025_02 sherlock_cork-protocol_2025_01 \
  --model "zai-org/GLM-5-TEE" \
  --output scoring_baseline/glm

python scripts/eval_agents.py \
  --agents agent317 agent794 \
  --projects code4rena_secondswap_2025_02 sherlock_oku_2024_12 \
           code4rena_bakerfi-invitational_2025_02 sherlock_cork-protocol_2025_01 \
  --model "MiniMaxAI/MiniMax-M2.5-TEE" \
  --output scoring_baseline/minimax
```

## Key File References

- `scripts/eval_agents.py` — Main eval loop (to be parallelized)
- `validator/scorer.py:64-69` — MODELS list and CHUTES_MODELS
- `validator/scorer.py:130-143` — prompt() payload + Kimi-specific overrides
- `validator/scorer.py:150-188` — Scorer retry logic (max_retries=4, linear backoff)
- `validator/scorer.py:192-194` — Token counter accumulation (not thread-safe)
- `validator/scorer.py:338-511` — Chunk iteration in find_match_in_results
- `validator/proxy/api.py:43-57` — Proxy semaphore + asyncio.to_thread dispatch
- `validator/proxy/chutes_client.py:22-27` — TIMEOUT, MAX_RETRIES, SESSION
- `validator/proxy/chutes_client.py:74-186` — call_chutes retry loop
- `validator/proxy/models.py:18` — temperature default (0.2)

## Existing Kimi vs GLM Scores (agent317, from prior runs)

| Project | Kimi TP | GLM TP | Expected | Agree? |
|---------|---------|--------|----------|--------|
| lambowin | 2 | 2 | 4 | Yes |
| secondswap | 1 | 3 | 3 | **No** |
| virtuals-protocol | 3 | 3 | 6 | Yes |
| cork-protocol | 0 | 2 | 11 | **No** |
| oku | 1 | 4 | 8 | **No** |
