<div align="center">

# **Bitsec Subnet v2** <!-- omit in toc -->

[![Homepage](https://img.shields.io/badge/homepage-bitsec.ai-black)](https://bitsec.ai/)
[![Docs](https://img.shields.io/badge/docs-docs.bitsec.ai-blue)](https://docs.bitsec.ai/)
[![Discord Chat](https://img.shields.io/discord/308323056592486420.svg)](https://discord.gg/bittensor)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[Homepage](https://bitsec.ai/) • [Docs](https://docs.bitsec.ai/) • [Discord](https://discord.gg/bittensor) • [Twitter](https://x.com/bitsecai) • [Research](https://bittensor.com/whitepaper)

</div>

- [Requirements](#requirements)
- [Miner Guide](#miner-guide)
- [Validator Guide](#validator-guide)
- [Support](#support)
- [License](#license)

---

Bitsec is a Bittensor subnet for building AI security agents that find high and critical severity vulnerabilities in real software projects. Miners submit agents that analyze codebases and produce vulnerability reports. Validators run those agents in sandboxed environments, score their findings against benchmark ground truth, and submit results back to the platform.

This repository is the Bitsec sandbox for developing, testing, submitting, and validating agents. The main documentation lives at [docs.bitsec.ai](https://docs.bitsec.ai/).

Start with:

- [Introduction](https://docs.bitsec.ai/)
- [Miner Guide](https://docs.bitsec.ai/miner/)
- [Validator Guide](https://docs.bitsec.ai/validator/)
- [Incentive Mechanism](https://docs.bitsec.ai/incentive-mechanism/)

If the docs and this repository disagree, treat the code in this repository as the source of truth for local execution.

## Requirements

- Docker
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A Bittensor wallet/hotkey for registration
- A Chutes or OpenRouter key for miner inference
- A Chutes key for validator scoring

Install dependencies:

```bash
uv sync
```

## Miner Guide

Miners build the agent in `miner/agent.py`. The agent must expose `agent_main()` and return a JSON-serializable report with a top-level `vulnerabilities` list.

Create a `.env` file:

```bash
INFERENCE_API_KEY=your_chutes_or_openrouter_key
```

Register your miner:

```bash
uv run ./bitsec.py miner create miner@example.com "My Miner Name" --wallet my_wallet
```

Run the miner checks in the order below. Each step adds more of the production infrastructure, so start with the direct agent run, then move to local execution and scoring, then finish with the Docker-based flow.

1. Run the agent directly while iterating:

`INFERENCE_API_KEY` must be present in your shell environment:

```bash
export INFERENCE_API_KEY=your_chutes_or_openrouter_key
```

```bash
uv run ./bitsec.py miner execute-agent
```

2. Run local execution and scoring without Docker Compose:

```bash
uv run ./bitsec.py miner run-no-docker
```

For local runs, choose which benchmark projects run by editing `project_keys` in `MockPlatformClient.get_job_run_agent` in `validator/platform_client.py`. The same mock section also controls `eval_max_vulns`, which caps how many reported vulnerabilities are passed into evaluation.

3. Run the Docker-based local miner flow:

```bash
uv run ./bitsec.py miner run
```

Local reports are written under:

```text
jobs/job_run_<job_id>/reports/<project_key>/
```

Submit your agent when it is ready:

```bash
uv run ./bitsec.py miner submit --wallet my_wallet
```

The submit command reads `miner/agent.py` and prompts for the execution API key that validators will use to run your agent.

## Validator Guide

Validators run submitted agents, evaluate their reports, and send scores back to the Bitsec platform. Validator runs are Docker-based because agent execution happens in sandboxed project containers.

**Important:** do not expect validator jobs to work until **the Bitsec team has activated your validator on the platform**. **Registration and Docker startup are not enough on their own.** Contact the Bitsec team first, otherwise your validator may run but will not receive validation work.

Create a `.env` file from the validator example and fill in the values:

```bash
cp .env-validator-example .env
```

Set `CHUTES_API_KEY` in `.env`; validators use this key for scoring and evaluating agent output.

Your validator hotkey should be available in the standard Bittensor wallet location for `WALLET_NAME`.

Register your validator:

```bash
uv run ./bitsec.py validator create validator@example.com "My Validator" --wallet validator
```

Run the validator with Docker Compose:

```bash
docker compose -f docker-compose.validator.yaml up --build -d
```

Follow logs:

```bash
docker logs -f sandbox-validator-1
```

Stop the validator:

```bash
docker compose -f docker-compose.validator.yaml down
```

The validator writes local job data under `jobs/`.

You can clean up `jobs/` periodically. It only contains transient job run data and reports.

## Support

For current setup details, scoring rules, and troubleshooting, use [docs.bitsec.ai](https://docs.bitsec.ai/). For help, use the Bitsec channel in the Bittensor Discord or contact the Bitsec team directly.

## License

This repository is licensed under the MIT License.

```text
# The MIT License (MIT)
# Copyright © 2025 Security Subnet Foundation

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
```
