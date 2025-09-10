<div align="center">

# **Bitsec Subnet v2** <!-- omit in toc -->

[![Discord Chat](https://img.shields.io/discord/308323056592486420.svg)](https://discord.gg/bittensor)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Security Agent MVP Design <!-- omit in toc -->

[Discord](https://discord.gg/bittensor) • [Network](https://taostats.io/) • [Research](https://bittensor.com/whitepaper)

</div>

---

- [MVP Design overview](#MVP-Design-overview)

---

## MVP Design overview

Scope: End-to-end demo: Miner makes agent → Validator evals codebase vulns → Score (just print rankings).

scabench do 1 vuln
sandbox: docker
miner submits simplified agents
validator runs 1 vuln
openai api

no bittensor components
no database
no logging
no / limited website
no chutes
no visibility

Platform API (Central Hub): A FastAPI server acting as the "subnet platform."

- Endpoint: /upload/agent (POST) – Accepts a single agent.py file upload. Validate it's Python, uses only stdlib (simple regex check). Store in local folder with metadata (e.g., miner_id as filename).
- Endpoint: /tasks (GET) – Returns a list of simple coding tasks (hardcoded: e.g., "Implement a function reverse_string(s: str) -> str").

Miner Script: A simple Python script or CLI.

- Takes a task prompt.
- Optionally uses LLM to generate code (e.g., prompt: "Write Python code for [task] as a function in agent.py").
- Uploads generated agent.py to the API.

Validator Script: The core evaluator (run manually or via cron-like loop).

- Pulls all submitted agents from local storage.
- For each agent and task:Spin up a Docker container.
- Copy agent.py into it, run python agent.py --task [prompt] (assume agents have a main function).
- Capture output, run basic tests (e.g., assert reverse_string("hello") == "olleh").

- Score: 0-100 based on tests passed (e.g., 100 if all pass).
- Output rankings to console/JSON (simulate "emissions/rewards").

Sandbox Execution: Use Docker SDK to:

- Create container: docker run -v [host_dir]:/app python:3.11-slim python /app/agent.py
- Limit resources (CPU/time) to prevent hangs.
- Handle errors gracefully (e.g., timeout after 10s).

Demo workflow:

1. Miner: Generate/submit 2-3 sample agents (one good, one bad).
2. Validator: Run eval, show scores like "Agent1: 100 (reward: 50 'TAO')".

Validator: Run eval, show scores like "Agent1: 100 (reward: 50 'TAO')".

## In order to simplify the building of subnets, this template abstracts away the complexity of the underlying blockchain and other boilerplate code. While the default behavior of the template is sufficient for a simple subnet, you should customize the template in order to meet your specific requirements.

```python
python -m venv env; source env/bin/activate;
pip install -U pip
pip install -r requirements.txt
```

## License

This repository is licensed under the MIT License.

```text
# The MIT License (MIT)
# Copyright © 2024 Opentensor Foundation

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
