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


MAX_WORKERS = 4
MAX_TOOL_TURNS = 25

console = Console()

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
        name = tool_call["function"]["name"]
        args = json.loads(tool_call["function"]["arguments"])

        if name == "list_files":
            return self._tool_list_files(source_dir, args["directory"])
        elif name == "read_file":
            return self._tool_read_file(source_dir, args["file_path"])
        elif name == "report_vulnerabilities":
            return json.dumps(args)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    def _tool_list_files(self, source_dir: Path, directory: str) -> str:
        """List files in directory, scoped to project root."""
        root = source_dir.resolve()
        target = (source_dir / directory).resolve()
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
        target = (source_dir / file_path).resolve()
        if not str(target).startswith(str(root)):
            return json.dumps({"error": "Access denied: path outside project"})
        if not target.is_file():
            return json.dumps({"error": f"Not a file: {file_path}"})
        try:
            content = target.read_text(encoding="utf-8")
            return content
        except Exception as e:
            return json.dumps({"error": str(e)})

    def analyze_project_with_tools(self, source_dir: Path, project_name: str) -> AnalysisResult:
        """Analyze a project using multi-turn tool use."""
        system_prompt = dedent("""\
            You are a senior smart contract security auditor. You have access to tools to explore a project directory and read source files. Your job is to:

            1. List files in the project to understand its structure
            2. Read relevant source files (focus on .sol, .vy, .cairo, .rs, .move files, skip tests/mocks)
            3. Analyze the code for security vulnerabilities
            4. Report your findings using the report_vulnerabilities tool

            Be thorough but focused. Look for common vulnerability patterns like reentrancy, access control issues, integer overflow, and logic errors.
        """)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Analyze the smart contract project in the root directory '.'. The project is: {project_name}"},
        ]

        all_vulnerabilities = []
        total_input_tokens = 0
        total_output_tokens = 0
        files_analyzed = set()
        reported = False

        for turn in range(MAX_TOOL_TURNS):
            # Force reporting on the last turn
            if turn == MAX_TOOL_TURNS - 1 and not reported:
                messages.append({
                    "role": "user",
                    "content": "You are running out of turns. Call report_vulnerabilities NOW with all vulnerabilities you have found so far.",
                })
                tool_choice = {"type": "function", "function": {"name": "report_vulnerabilities"}}
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

            # Append assistant message (with tool_calls) to conversation
            messages.append(message)

            # Execute each tool call and append results
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

            if reported:
                break

        # Deduplicate
        unique = {v.id: v for v in all_vulnerabilities}
        vulns = list(unique.values())

        return AnalysisResult(
            project=project_name,
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
        
        Returns:
            Tuple of (vulnerabilities, input_tokens, output_tokens)
        """
        file_path = Path(relative_path)

        console.print(f"[dim]  → Analyzing {relative_path} ({len(content)} bytes)[/dim]")

        from langchain_core.output_parsers import PydanticOutputParser
        parser = PydanticOutputParser(pydantic_object=Vulnerabilities)
        format_instructions = parser.get_format_instructions()

        system_prompt = dedent(f"""
            You are a security auditor analyzing smart contract code for vulnerabilities.

            Analyze the provided code file and identify security vulnerabilities. For each vulnerability found, provide:

            1. A clear title describing the issue
            2. A detailed description including:
               - What the vulnerability is
               - Where it occurs (function name, line references)
               - Why it's a security issue
               - Potential impact
            3. The vulnerability type (e.g., reentrancy, access control, integer overflow, etc.)
            4. Severity level (critical, high, medium, low)
            5. Confidence level (0.0 to 1.0)

            Focus on REAL security issues that could lead to:
            - Loss of funds
            - Unauthorized access
            - Denial of service
            - Data corruption
            - Privilege escalation
            - Protocol manipulation

            DO NOT report:
            - Code quality issues without security impact
            - Gas optimization suggestions unless they prevent DoS
            - Style or naming convention issues
            - Missing comments or documentation
            - Theoretical issues without practical exploit paths

            IMPORTANT: Your response must be ONLY the raw valid JSON object, without any markdown formatting, comments, or other text.

            Do not add any explanations or markdown formatting (e.g., ```json) to the output.

            {format_instructions}

            IMPORTANT: Begin your response with `{{"vulnerabilities":`
        """)

        user_prompt = dedent(f"""
            Analyze this {file_path.suffix} file for security vulnerabilities:

            File: {relative_path}
            ```{file_path.suffix[1:] if file_path.suffix else 'txt'}
            {content}
            ```

            Identify and report security vulnerabilities found.
        """)

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            response = self.inference(messages=messages)
            response_content = (response['choices'][0]['message'].get('content') or '').strip()

            msg_json = self.clean_json_response(response_content)

            vulnerabilities = Vulnerabilities(**msg_json)
            for v in vulnerabilities.vulnerabilities:
                v.reported_by_model = self.config['model']

            if vulnerabilities:
                console.print(f"[green]  → Found {len(vulnerabilities.vulnerabilities)} vulnerabilities[/green]")
            else:
                console.print("[yellow]  → No vulnerabilities found[/yellow]")

            usage = response.get('usage', {})
            input_tokens = usage.get('prompt_tokens', 0)
            output_tokens = usage.get('completion_tokens', 0)

            return vulnerabilities, input_tokens, output_tokens
            
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
        
        # Find files to analyze
        if file_patterns:
            files = []
            for pattern in file_patterns:
                files.extend(source_dir.glob(pattern))

        else:
            # Default to common smart contract patterns
            patterns = ['**/*.sol', '**/*.vy', '**/*.cairo', '**/*.rs', '**/*.move']
            files = []
            for pattern in patterns:
                files.extend(source_dir.glob(pattern))
        
        # Remove duplicates and filter
        exclude_dirs = {"testing", "mocks", "examples"}
        files = set(files)
        files = [
            f for f 
            in files 
            if f.is_file() and 'test' not in f.name.lower()
            and not any(part.lower() in exclude_dirs for part in f.parts)
        ]

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
        'model': "deepseek-ai/DeepSeek-V3.1-Terminus"
    }

    if not project_dir:
        project_dir = "/app/project_code"

    console.print(Panel.fit(
        "[bold cyan]SCABENCH BASELINE RUNNER[/bold cyan]\n"
        f"[dim]Model: {config['model']}[/dim]\n",
        border_style="cyan"
    ))

    try:
        runner = BaselineRunner(config, inference_api)

        source_dir = Path(project_dir) if project_dir else None
        if not source_dir or not source_dir.exists() or not source_dir.is_dir():
            console.print(f"[red]Error: Invalid source directory: {project_dir}[/red]")
            sys.exit(1)
        
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
    report = agent_main('projects/code4rena_secondswap_2025_02', inference_api=inference_api)
