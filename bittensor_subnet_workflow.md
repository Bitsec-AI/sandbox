# Bitsec Subnet v2 MVP Workflow Diagram

This Mermaid.js diagram shows the simplified MVP workflow for the Security Agent system as described in the README.md.

```mermaid
graph TB
    %% User Types
    M[Miner Script<br/>🐍 Python CLI<br/>📝 Takes task prompt<br/>🤖 Uses LLM for code generation<br/>📤 Uploads agent.py]
    V[Validator Script<br/>🔍 Core evaluator<br/>📊 Runs manually/cron<br/>🐳 Manages Docker containers<br/>📈 Scores & ranks agents]

    %% Core Components
    P[Platform API<br/>🌐 FastAPI Central Hub<br/>📤 /upload/agent POST<br/>📋 /tasks GET<br/>💾 Local file storage]
    DOCKER[Docker Sandbox<br/>🐳 Code execution<br/>⏱️ 10s timeout limit<br/>🛡️ Resource constraints<br/>📦 python:3.11-slim]
    LLM[LLM Services<br/>🧠 Code generation<br/>📝 "Write Python code for [task]"<br/>💭 AI assistance]

    %% Tasks & Storage
    TASKS[Hardcoded Tasks<br/>📋 reverse_string function<br/>🔧 Simple coding challenges<br/>✅ Test assertions]
    STORAGE[Local Storage<br/>📁 agent.py files<br/>🏷️ miner_id as filename<br/>📊 Metadata tracking]

    %% Workflow Connections
    M -->|1. Takes task prompt| TASKS
    M -->|2. Generates code| LLM
    LLM -->|3. Returns agent.py| M
    M -->|4. Uploads agent.py| P
    P -->|5. Validates Python/stdlib| P
    P -->|6. Stores with metadata| STORAGE

    %% Validation Flow
    V -->|7. Pulls all agents| STORAGE
    V -->|8. Gets task list| P
    V -->|9. For each agent + task| V
    V -->|10. Spins up container| DOCKER
    DOCKER -->|11. Copies agent.py| DOCKER
    DOCKER -->|12. Runs python agent.py --task| DOCKER
    DOCKER -->|13. Captures output| V
    V -->|14. Runs test assertions| V
    V -->|15. Scores 0-100| V
    V -->|16. Outputs rankings| V

    %% Demo Workflow
    M -->|17. Generate 2-3 agents<br/>(good & bad examples)| M
    V -->|18. Run evaluation| V
    V -->|19. Show scores like<br/>"Agent1: 100 (reward: 50 'TAO')"| V

    %% Styling
    classDef userType fill:#e1f5fe,stroke:#01579b,stroke-width:2px
    classDef coreComponent fill:#f3e5f5,stroke:#4a148c,stroke-width:2px
    classDef external fill:#fff3e0,stroke:#e65100,stroke-width:2px
    classDef storage fill:#e8f5e8,stroke:#1b5e20,stroke-width:2px

    class M,V userType
    class P coreComponent
    class DOCKER,LLM external
    class TASKS,STORAGE storage
```

## MVP Design Overview

**Scope**: End-to-end demo: Submit agent → Evaluate on task → Score and "reward" (just print rankings)

### Key Features (Simplified)

- ❌ **No Bittensor components**
- ❌ **No database**
- ❌ **No logging**
- ❌ **No/limited website**
- ✅ **Docker sandbox execution**
- ✅ **Simple agent submission**
- ✅ **Basic scoring system**

## Workflow Steps

### 1. Agent Generation & Submission

1. **Miner** takes a task prompt
2. **LLM** generates Python code for the task
3. **Miner** uploads `agent.py` to Platform API
4. **Platform API** validates it's Python with stdlib only
5. **Platform API** stores with `miner_id` as filename

### 2. Validation & Scoring

1. **Validator** pulls all submitted agents from storage
2. **Validator** gets task list from Platform API
3. **Validator** spins up Docker container for each agent
4. **Docker** copies `agent.py` and runs `python agent.py --task [prompt]`
5. **Validator** captures output and runs test assertions
6. **Validator** scores 0-100 based on tests passed
7. **Validator** outputs rankings to console/JSON

### 3. Demo Workflow

1. **Miner** generates 2-3 sample agents (one good, one bad)
2. **Validator** runs evaluation
3. **Validator** shows scores like "Agent1: 100 (reward: 50 'TAO')"

## Platform API Endpoints

- **POST /upload/agent**: Accepts single `agent.py` file upload
  - Validates it's Python
  - Checks uses only stdlib (regex check)
  - Stores in local folder with metadata
- **GET /tasks**: Returns hardcoded coding tasks
  - Example: "Implement a function reverse_string(s: str) -> str"

## Docker Sandbox Execution

```bash
docker run -v [host_dir]:/app python:3.11-slim python /app/agent.py
```

- **Resource limits**: CPU/time to prevent hangs
- **Timeout**: 10 seconds maximum
- **Error handling**: Graceful timeout handling
- **Security**: Isolated execution environment

## Scoring System

- **0-100 scale** based on tests passed
- **100 points** if all tests pass
- **Console/JSON output** for rankings
- **Simulated rewards** (just printed, no real TAO)

## Security Features

- **Docker isolation** for code execution
- **Resource limits** to prevent system abuse
- **Timeout handling** for graceful error recovery
- **Python stdlib validation** for security
