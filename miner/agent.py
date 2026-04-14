import hashlib
import json
import os
import requests
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from textwrap import dedent


from pydantic import BaseModel
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn
from rich.panel import Panel


MAX_WORKERS = 2
MAX_TOOL_PASS_WORKERS = 16
MAX_TOOL_RUNTIME_SECONDS = 5 * 60
DEFAULT_CONTRACT_FILE_PATTERNS = ['**/*.sol', '**/*.vy', '**/*.cairo', '**/*.rs', '**/*.move']
EXCLUDE_DIRS = {"testing", "mocks", "examples", "interfaces", "script", "broadcast", "libraries"}

console = Console()

REASONING_MODELS = {
    "tngtech/DeepSeek-TNG-R1T2-Chimera-TEE",
}
JSON_MODEL = "MiniMaxAI/MiniMax-M2.5-TEE"

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory. Returns file paths relative to the project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory path relative to project root. Use '.' for the root directory."
                    }
                },
                "required": ["directory"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to the project root."
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "report_vulnerabilities",
            "description": "Report security vulnerabilities found in the project. Call this when you have completed your analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vulnerabilities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Short title of the vulnerability"},
                                "description": {"type": "string", "description": "Detailed description of the vulnerability and its impact"},
                                "vulnerability_type": {"type": "string", "description": "Category (e.g., reentrancy, overflow, access-control)"},
                                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                                "confidence": {"type": "number", "description": "Confidence score 0.0-1.0"},
                                "location": {"type": "string", "description": "Specific location (e.g., function name, line range)"},
                                "file": {"type": "string", "description": "File path where the vulnerability was found"}
                            },
                            "required": ["title", "description", "vulnerability_type", "severity", "confidence", "location", "file"]
                        }
                    }
                },
                "required": ["vulnerabilities"]
            }
        }
    }
]


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class Vulnerability(BaseModel):
    """A security vulnerability finding."""
    title: str
    description: str
    vulnerability_type: str
    severity: Severity
    confidence: float
    location: str
    file: str
    id: str | None = None
    reported_by_model: str = ""
    status: str = "proposed"

    def __init__(self, **data):
        super().__init__(**data)
        if not self.id:
            id_source = f"{self.file}:{self.title}"
            self.id = hashlib.md5(id_source.encode()).hexdigest()[:16]

class Vulnerabilities(BaseModel):
    """A collection of security vulnerability vulnerabilities."""
    vulnerabilities: list[Vulnerability]

class AnalysisResult(BaseModel):
    """Result from analyzing a project."""
    project: str
    timestamp: str
    files_analyzed: int
    files_skipped: int
    total_vulnerabilities: int
    vulnerabilities: list[Vulnerability]
    token_usage: dict[str, int]


class BaselineRunner:
    def __init__(self, config: dict[str, Any] | None = None, inference_api: str = None):
        self.config = config or {}
        self.model = self.config['model']
        self.inference_api = inference_api or os.getenv('INFERENCE_API', "http://bitsec_proxy:8000")
        self.project_id = os.getenv('PROJECT_ID', "local")
        self.job_id = os.getenv('JOB_ID', "local")
        self.chutes_api_key = os.getenv('CHUTES_API_KEY')

        console.print(f"Inference: {self.inference_api}")

    def inference(self, messages: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
        payload = {
            "model": self.config['model'],
            "messages": messages,
            "response_format": {"type": "json_object"},
        }

        # Allow callers to override/add fields (tools, tool_choice, response_format)
        payload.update(kwargs)

        headers = {
            "x_project_id": self.project_id or "local",
            "x_job_id": self.job_id,
            "x-chutes-api-key": self.chutes_api_key,
        }

        resp = None
        try:
            inference_url = f"{self.inference_api}/inference"
            resp = requests.post(
                inference_url,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()

        except requests.exceptions.HTTPError as e:
            # This prevents the AttributeError when requests.post() raises a RequestException before returning
            if resp is not None:
                try:
                    error_detail = resp.json()
                except (ValueError, AttributeError):
                    error_detail = resp.text if hasattr(resp, 'text') else str(resp)
            else:
                error_detail = "No response received"
            console.print(f"Inference Proxy Error: {e} {error_detail}")
            raise

        except requests.exceptions.RequestException as e:
            if resp is not None:
                try:
                    error_detail = resp.json()
                except (ValueError, AttributeError):
                    error_detail = resp.text if hasattr(resp, 'text') else str(resp)
            else:
                error_detail = "No response received"
            console.print(f"Inference Error: {e} {error_detail}")
            raise

        return resp.json()

    def clean_json_response(self, response_content: str) -> dict[str, Any]:
        while response_content.startswith("_\n"):
            response_content = response_content[2:]

        response_content = response_content.strip()

        if response_content.startswith("return"):
            response_content = response_content[6:]

        response_content = response_content.strip()

        # Remove code block markers if present
        if response_content.startswith("```") and response_content.endswith("```"):
            lines = response_content.splitlines()

            if lines[0].startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            response_content = "\n".join(lines).strip()

        resp_json = json.loads(response_content)

        return resp_json

    def _execute_tool_call(self, tool_call: dict, source_dir: Path) -> str:
        """Execute a tool call and return the result as a string."""
        try:
            function = tool_call.get("function", {})
            name = function.get("name")
            raw_arguments = function.get("arguments", "{}")
            args = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid tool arguments JSON: {exc}"})

        if name == "list_files":
            directory = args.get("directory")
            if not isinstance(directory, str):
                return json.dumps({"error": "Missing or invalid required argument: directory"})
            return self._tool_list_files(source_dir, directory)
        elif name == "read_file":
            file_path = args.get("file_path")
            if not isinstance(file_path, str):
                return json.dumps({"error": "Missing or invalid required argument: file_path"})
            return self._tool_read_file(source_dir, file_path)
        elif name == "report_vulnerabilities":
            if not isinstance(args.get("vulnerabilities"), list):
                return json.dumps({"error": "Missing or invalid required argument: vulnerabilities"})
            return json.dumps(args)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    def _tool_list_files(self, source_dir: Path, directory: str) -> str:
        """List files in directory, scoped to project root."""
        root = source_dir.resolve()
        target = (source_dir / directory.replace(" ", "")).resolve()
        if not str(target).startswith(str(root)):
            return json.dumps({"error": "Access denied: path outside project"})
        if not target.is_dir():
            return json.dumps({"error": f"Not a directory: {directory}"})
        files = []
        for item in sorted(target.iterdir()):
            rel = str(item.resolve().relative_to(root))
            files.append(rel + ("/" if item.is_dir() else ""))
        return json.dumps({"files": files})

    def _tool_read_file(self, source_dir: Path, file_path: str) -> str:
        """Read file contents, scoped to project root."""
        root = source_dir.resolve()
        target = (source_dir / file_path.replace(" ", "")).resolve()
        if not str(target).startswith(str(root)):
            return json.dumps({"error": "Access denied: path outside project"})
        if not target.is_file():
            console.log(f"Not a file: {file_path}")
            return json.dumps({"error": f"Not a file: {file_path}"})
        try:
            content = target.read_text(encoding="utf-8")
            return content
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _discover_contract_files(
        self,
        source_dir: Path,
        file_patterns: list[str] | None = None,
    ) -> list[Path]:
        """Discover contract-like source files under the project root."""
        patterns = file_patterns or DEFAULT_CONTRACT_FILE_PATTERNS
        files = []
        for pattern in patterns:
            files.extend(source_dir.glob(pattern))

        unique_files = {
            file_path for file_path in files
            if file_path.is_file()
            and "test" not in file_path.name.lower()
            and not any(part.lower() in EXCLUDE_DIRS for part in file_path.parts)
        }

        return sorted(unique_files, key=lambda path: str(path.relative_to(source_dir)))

    def _seed_file_context(
        self,
        messages: list[dict[str, Any]],
        source_dir: Path,
        relative_path: str,
        tool_call_id: str,
    ) -> list[dict[str, Any]]:
        """Seed a conversation with synthetic list_files and read_file tool calls."""
        list_tool_call_id = f"{tool_call_id}-list"
        messages.append({
            "role": "assistant",
            "tool_calls": [{
                "id": list_tool_call_id,
                "type": "function",
                "function": {
                    "name": "list_files",
                    "arguments": json.dumps({"directory": "."}),
                },
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": list_tool_call_id,
            "content": self._tool_list_files(source_dir, "."),
        })
        messages.append({
            "role": "assistant",
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"file_path": relative_path}),
                },
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": self._tool_read_file(source_dir, relative_path),
        })
        return messages

    def _run_tool_analysis_pass(
        self,
        source_dir: Path,
        messages: list[dict[str, Any]],
        deadline: float,
        files_analyzed: set[str],
        all_vulnerabilities: list[Vulnerability],
        total_input_tokens: int,
        total_output_tokens: int,
        relative_path: str,
    ) -> tuple[list[dict[str, Any]], bool, int, int]:
        """Run one tool-use analysis pass within the provided deadline."""
        reported = False
        forced_report_requested = False
        turn = 0

        while True:
            turn += 1

            if (time.monotonic() >= deadline or turn >= 2) and not reported:
                if forced_report_requested:
                    break
                messages.append({
                    "role": "user",
                    "content": "You are running out of time. Call report_vulnerabilities NOW with all vulnerabilities you have found so far.",
                })
                console.print(f"Reporting on file: {relative_path}")
                tool_choice = {"type": "function", "function": {"name": "report_vulnerabilities"}}
                forced_report_requested = True
            else:
                tool_choice = "auto"

            response = self.inference(
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice=tool_choice,
                response_format={"type": "text"},
            )

            usage = response.get("usage", {})
            total_input_tokens += usage.get("prompt_tokens", 0)
            total_output_tokens += usage.get("completion_tokens", 0)

            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                break

            messages.append(message)

            for tc in tool_calls:
                result_str = self._execute_tool_call(tc, source_dir)

                if tc["function"]["name"] == "read_file":
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        files_analyzed.add(args.get("file_path", ""))
                    except Exception:
                        pass

                if tc["function"]["name"] == "report_vulnerabilities":
                    reported = True
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        for v_data in args.get("vulnerabilities", []):
                            v_data["reported_by_model"] = self.config["model"]
                            v = Vulnerability(**v_data)
                            all_vulnerabilities.append(v)
                    except Exception as e:
                        console.print(f"[red]Error parsing report_vulnerabilities: {e}[/red]")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })

            if reported or forced_report_requested:
                break

        return messages, reported, total_input_tokens, total_output_tokens

    def _analyze_target_file_with_tools(
        self,
        source_dir: Path,
        relative_path: str,
        deadline: float,
        system_prompt: str,
        tool_call_id: str,
    ) -> tuple[set[str], list[Vulnerability], int, int]:
        """Run one seeded tool-use analysis pass for a single target file."""
        if time.monotonic() >= deadline:
            return set(), [], 0, 0

        console.print(f"Analyzing file: {relative_path}")

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Analyze the smart contract project in the root directory '.'. "
                    f"Focus this pass on: {relative_path}. "
                    "The target file has already been provided via a read_file tool result. "
                    "Use more tool calls only if you need extra context from other files before reporting vulnerabilities."
                    "Hard rule: after the provided target file, you may inspect at most one additional file. Never request more than one extra file. After that single extra file, you must call report_vulnerabilities."
                    "Prefer the most security-relevant related file: auth, upgrade, accounting, vault, router, manager, oracle, or library logic directly used by the vulnerable path."
                ),
            },
        ]
        messages = self._seed_file_context(
            messages=messages,
            source_dir=source_dir,
            relative_path=relative_path,
            tool_call_id=tool_call_id,
        )

        files_analyzed = {relative_path}
        all_vulnerabilities: list[Vulnerability] = []
        _, _, total_input_tokens, total_output_tokens = self._run_tool_analysis_pass(
            source_dir=source_dir,
            messages=messages,
            deadline=deadline,
            files_analyzed=files_analyzed,
            all_vulnerabilities=all_vulnerabilities,
            total_input_tokens=0,
            total_output_tokens=0,
            relative_path=relative_path,
        )
        return files_analyzed, all_vulnerabilities, total_input_tokens, total_output_tokens

    def analyze_project_with_tools(self, source_dir: Path, project_name: str | None = None) -> AnalysisResult:
        """Analyze a project using local file discovery plus per-file tool-use passes."""
        project_label = project_name or source_dir.name
        system_prompt = dedent("""\
            You are a senior smart contract security auditor. You have access to tools to explore a project directory and read source files. Your job is to:

            1. Analyze one target contract file at a time
            2. Use the provided read_file tool result for the current target file as your starting point
            3. Call additional tools only if you need more project context from related files
            4. Report your findings using the report_vulnerabilities tool before the pass ends

            Be thorough but focused. Look for common vulnerability patterns (but not limited to) such as reentrancy, access control issues, integer overflow, and logic errors.
            Focus on REAL security issues: loss of funds, unauthorized access, denial of service, privilege escalation, protocol manipulation.
        """)

        contract_files = self._discover_contract_files(source_dir)
        if not contract_files:
            return AnalysisResult(
                project=project_label,
                timestamp=datetime.now().isoformat(),
                files_analyzed=0,
                files_skipped=0,
                total_vulnerabilities=0,
                vulnerabilities=[],
                token_usage={
                    "total_input": 0,
                    "total_output": 0,
                },
            )

        all_vulnerabilities = []
        total_input_tokens = 0
        total_output_tokens = 0
        files_analyzed = set()
        deadline = time.monotonic() + MAX_TOOL_RUNTIME_SECONDS

        with ThreadPoolExecutor(max_workers=MAX_TOOL_PASS_WORKERS) as executor:
            futures = {
                executor.submit(
                    self._analyze_target_file_with_tools,
                    source_dir,
                    str(file_path.relative_to(source_dir)),
                    deadline,
                    system_prompt,
                    f"seed-read-{index}",
                ): file_path
                for index, file_path in enumerate(contract_files)
            }

            for future in as_completed(futures):
                analyzed_files, vulnerabilities, input_tokens, output_tokens = future.result()
                files_analyzed.update(analyzed_files)
                all_vulnerabilities.extend(vulnerabilities)
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

        # Deduplicate
        unique = {v.id: v for v in all_vulnerabilities}
        vulns = list(unique.values())

        return AnalysisResult(
            project=project_label,
            timestamp=datetime.now().isoformat(),
            files_analyzed=len(files_analyzed),
            files_skipped=0,
            total_vulnerabilities=len(vulns),
            vulnerabilities=vulns,
            token_usage={
                "total_input": total_input_tokens,
                "total_output": total_output_tokens,
            },
        )

    def analyze_file(self, relative_path: str, content: str) -> tuple[Vulnerabilities, int, int]:
        """Analyze a single file for security vulnerabilities.

        For reasoning models (R1): two-step approach — R1 reasons in text mode (user-only),
        then V3 converts analysis to structured JSON (system+user).
        For non-reasoning models: single call with system+user and json_object mode.

        Returns:
            Tuple of (vulnerabilities, input_tokens, output_tokens)
        """
        file_path = Path(relative_path)
        is_reasoning = self.config['model'] in REASONING_MODELS

        console.print(f"[dim]  -> Analyzing {relative_path} ({len(content)} bytes)[/dim]")

        total_input_tokens = 0
        total_output_tokens = 0

        try:
            if is_reasoning:
                # Step 1: R1 reasoning analysis (text mode, user-only — R1 has no system role)
                analysis_prompt = dedent(f"""\
                    You are a senior smart contract security auditor. Analyze this code file for security vulnerabilities.

                    File: {relative_path}
                    ```{file_path.suffix[1:] if file_path.suffix else 'txt'}
                    {content}
                    ```

                    Focus on REAL security issues: loss of funds, unauthorized access, denial of service, privilege escalation, protocol manipulation.
                    DO NOT report: code quality issues, gas optimizations, style issues, missing comments, theoretical issues without practical exploit paths.

                    For each vulnerability found, describe: title, what it is, where it occurs (function/line), why it's a security issue, impact, type, severity (critical/high/medium/low), and confidence (0.0-1.0).
                """)

                response = None
                for attempt in range(5):
                    try:
                        response = self.inference(
                            messages=[{"role": "user", "content": analysis_prompt}],
                            response_format={"type": "text"},
                        )
                        break
                    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
                        if attempt < 4:
                            wait = 3 * (attempt + 1)
                            console.print(f"[yellow]  -> Retry {attempt+1}/4 for R1 step on {relative_path} (wait {wait}s)[/yellow]")
                            time.sleep(wait)
                        else:
                            raise

                if response is None:
                    raise RuntimeError(f"R1 analysis failed after retries for {relative_path}")

                usage = response.get("usage", {})
                total_input_tokens += usage.get("prompt_tokens", 0)
                total_output_tokens += usage.get("completion_tokens", 0)

                reasoning_tokens = usage.get("reasoning_tokens", 0)
                if reasoning_tokens:
                    console.print(f"[dim]  -> Reasoning tokens: {reasoning_tokens}[/dim]")

                analysis_text = (response["choices"][0]["message"].get("content") or "").strip()

                # Step 2: V3 JSON structuring (json mode, system+user) — retry on 502
                json_system = dedent("""\
                    Convert the security audit analysis into a JSON object. Extract every vulnerability mentioned.
                    Respond with ONLY a JSON object: {"vulnerabilities": [{"title": "...", "description": "...", "vulnerability_type": "...", "severity": "critical|high|medium|low", "confidence": 0.0-1.0, "location": "...", "file": "..."}]}
                    If no vulnerabilities were found, respond with: {"vulnerabilities": []}
                """)

                json_user = f"Analysis of {relative_path}:\n\n{analysis_text}"

                json_response = None
                for attempt in range(5):
                    try:
                        json_response = self.inference(
                            messages=[
                                {"role": "system", "content": json_system},
                                {"role": "user", "content": json_user},
                            ],
                            model=JSON_MODEL,
                        )
                        break
                    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
                        if attempt < 4:
                            wait = 3 * (attempt + 1)
                            console.print(f"[yellow]  -> Retry {attempt+1}/4 for JSON step on {relative_path} (wait {wait}s)[/yellow]")
                            time.sleep(wait)
                        else:
                            raise

                if json_response is None:
                    raise RuntimeError(f"JSON structuring failed after retries for {relative_path}")

                usage2 = json_response.get("usage", {})
                total_input_tokens += usage2.get("prompt_tokens", 0)
                total_output_tokens += usage2.get("completion_tokens", 0)

                response_content = (json_response["choices"][0]["message"].get("content") or "").strip()

            else:
                # Non-reasoning: single call with system+user, json mode
                system_prompt = dedent(f"""\
                    You are a security auditor analyzing smart contract code for vulnerabilities.
                    Focus on REAL security issues: loss of funds, unauthorized access, denial of service, privilege escalation, protocol manipulation.
                    DO NOT report: code quality issues, gas optimizations, style issues, missing comments, theoretical issues without practical exploit paths.
                    Respond with ONLY a JSON object: {{"vulnerabilities": [{{"title": "...", "description": "...", "vulnerability_type": "...", "severity": "critical|high|medium|low", "confidence": 0.0-1.0, "location": "...", "file": "..."}}]}}
                    If no vulnerabilities are found, respond with: {{"vulnerabilities": []}}
                """)

                user_prompt = dedent(f"""\
                    Analyze this file for security vulnerabilities:

                    File: {relative_path}
                    ```{file_path.suffix[1:] if file_path.suffix else 'txt'}
                    {content}
                    ```
                """)

                response = self.inference(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )

                usage = response.get("usage", {})
                total_input_tokens += usage.get("prompt_tokens", 0)
                total_output_tokens += usage.get("completion_tokens", 0)

                response_content = (response["choices"][0]["message"].get("content") or "").strip()

            msg_json = self.clean_json_response(response_content)
            vulnerabilities = Vulnerabilities(**msg_json)
            for v in vulnerabilities.vulnerabilities:
                v.reported_by_model = self.config["model"]

            if vulnerabilities.vulnerabilities:
                console.print(f"[green]  -> Found {len(vulnerabilities.vulnerabilities)} vulnerabilities[/green]")
            else:
                console.print("[yellow]  -> No vulnerabilities found[/yellow]")

            return vulnerabilities, total_input_tokens, total_output_tokens

        except Exception as e:
            console.print(f"[red]Error analyzing {file_path.name}: {e}[/red]")
            return Vulnerabilities(vulnerabilities=[]), 0, 0

    def process_file(self, file_path, source_dir):
        relative_path = str(file_path.relative_to(source_dir))

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if not content.strip():
                return "skipped", None

            vulnerabilities, input_tokens, output_tokens = self.analyze_file(
                relative_path, content
            )

            return "ok", (
                vulnerabilities.vulnerabilities,
                input_tokens,
                output_tokens,
            )

        except Exception as e:
            return "error", (file_path.name, e)

    def analyze_project(
        self, 
        source_dir: Path,
        project_name: str,
        file_patterns: list[str] | None = None
    ) -> AnalysisResult:
        """Analyze a project for security vulnerabilities.
        
        Args:
            source_dir: Directory containing source files
            project_name: Name of the project
            file_patterns: List of glob patterns for files to analyze
            
        Returns:
            AnalysisResult with vulnerabilities
        """
        console.print("\n[bold cyan]Analyzing project[/bold cyan]")
        
        files = self._discover_contract_files(source_dir, file_patterns)

        if not files:
            console.print("[yellow]No files found to analyze[/yellow]")
            return AnalysisResult(
                project=project_name,
                timestamp=datetime.now().isoformat(),
                files_analyzed=0,
                files_skipped=0,
                total_vulnerabilities=0,
                vulnerabilities=[],
                token_usage={'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
            )

        console.print(f"[dim]Found {len(files)} files to analyze[/dim]")

        # Analyze files
        all_vulnerabilities = []
        files_analyzed = 0
        files_skipped = 0
        total_input_tokens = 0
        total_output_tokens = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=console,
            transient=False
        ) as progress:
            task = progress.add_task(f"Analyzing {len(files)} files...", total=len(files))

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(self.process_file, file_path, source_dir): file_path
                    for file_path in files
                }

                for future in as_completed(futures):
                    result_type, result = future.result()

                    if result_type == "ok":
                        vulns, in_tok, out_tok = result
                        all_vulnerabilities.extend(vulns)
                        files_analyzed += 1
                        total_input_tokens += in_tok
                        total_output_tokens += out_tok

                    elif result_type == "skipped":
                        files_skipped += 1

                    elif result_type == "error":
                        filename, err = result
                        console.print(f"[red]Error processing {filename}: {err}[/red]")
                        files_skipped += 1

                    progress.advance(task)

        # Deduplicate vulnerabilities
        unique_vulnerabilities = {
            v.id: v for v in all_vulnerabilities
        }
        vulns = list(unique_vulnerabilities.values())
        
        result = AnalysisResult(
            project=project_name,
            timestamp=datetime.now().isoformat(),
            files_analyzed=files_analyzed,
            files_skipped=files_skipped,
            total_vulnerabilities=len(unique_vulnerabilities),
            vulnerabilities=vulns,
            token_usage={
                'input_tokens': total_input_tokens,
                'output_tokens': total_output_tokens,
                'total_tokens': total_input_tokens + total_output_tokens
            }
        )

        self.print_summary(result)
        
        return result

    def print_summary(self, result: AnalysisResult):
        """Print analysis summary."""
        console.print(f"\n[bold]Summary for {result.project}:[/bold]")
        console.print(f"  Files analyzed: {result.files_analyzed}")
        console.print(f"  Files skipped: {result.files_skipped}")
        console.print(f"  Total vulnerabilities: {result.total_vulnerabilities}")
        console.print(f"  Token usage: {result.token_usage['total_tokens']:,}")
        console.print(f"    Input tokens: {result.token_usage['input_tokens']:,}")
        console.print(f"    Output tokens: {result.token_usage['output_tokens']:,}")

        if result.vulnerabilities:
            # Count by severity
            severity_counts = {}
            for vulnerability in result.vulnerabilities:
                sev = vulnerability.severity
                severity_counts[sev] = severity_counts.get(sev, 0) + 1

            console.print("  By severity:")
            for sev in [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]:
                if sev.value in severity_counts:
                    color = {
                        Severity.CRITICAL: 'red',
                        Severity.HIGH: 'orange1',
                        Severity.MEDIUM: 'yellow',
                        Severity.LOW: 'green'
                    }[sev]
                    console.print(f"    [{color}]{sev.value.capitalize()}:[/{color}] {severity_counts[sev.value]}")

    def save_result(self, result: AnalysisResult, output_file: str = "agent_report.json"):
        result_dict = result.model_dump()

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result_dict, f, indent=2)

        console.print(f"\n[green]Results saved to: {output_file}[/green]")
        return output_file


def agent_main(project_dir: str = None, inference_api: str = None):
    config = {
        'model': "MiniMaxAI/MiniMax-M2.5-TEE", # reasoning model
        # 'model': 'deepseek-ai/DeepSeek-V3.1-Terminus', # tool use model
    }

    if not project_dir:
        project_dir = "/app/project_code"

    is_reasoning = config['model'] in REASONING_MODELS

    console.print(Panel.fit(
        "[bold cyan]SCABENCH BASELINE RUNNER[/bold cyan]\n"
        f"[dim]Model: {config['model']}[/dim]\n"
        f"[dim]Mode: {'reasoning (file-by-file)' if is_reasoning else 'tool-use'}[/dim]\n",
        border_style="cyan"
    ))

    try:
        runner = BaselineRunner(config, inference_api)

        source_dir = Path(project_dir) if project_dir else None
        if not source_dir or not source_dir.exists() or not source_dir.is_dir():
            console.print(f"[red]Error: Invalid source directory: {project_dir}[/red]")
            sys.exit(1)

        if is_reasoning:
            result = runner.analyze_project(
                source_dir=source_dir,
                project_name=project_dir,
            )
        else:
            result = runner.analyze_project_with_tools(
                source_dir=source_dir,
                project_name=project_dir,
            )
        
        output_file = runner.save_result(result)
        
        # Final summary
        console.print("\n" + ("=" * 60))
        console.print(Panel.fit(
            f"[bold green]ANALYSIS COMPLETE[/bold green]\n\n"
            f"Project: {result.project}\n"
            f"Files analyzed: {result.files_analyzed}\n"
            f"Total vulnerabilities: {result.total_vulnerabilities}\n"
            f"Results saved to: {output_file}",
            border_style="green"
        ))

        return result.model_dump(mode="json")
        
    except ValueError as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        sys.exit(1)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        print(traceback.print_exc())
        sys.exit(1)


if __name__ == '__main__':
    from scripts.projects import fetch_projects
    from validator.manager import SandboxManager

    SandboxManager(is_local=True)
    time.sleep(10) # wait for proxy to start
    fetch_projects()
    inference_api = 'http://localhost:8087'
    report = agent_main('projects/code4rena_virtuals-protocol_2025_08', inference_api=inference_api)
