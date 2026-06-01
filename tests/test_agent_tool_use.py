"""Tests for the tool-use agent loop in miner/agent.py.

Mocks the inference API to simulate a multi-turn conversation where the model
calls list_files, read_file, and report_vulnerabilities in sequence.
"""
import json
import os
import tempfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from miner.agent import (
    BaselineRunner,
    AnalysisResult,
    MAX_TOOL_PASS_WORKERS,
    MAX_TOOL_RUNTIME_SECONDS,
    TOOL_DEFINITIONS,
    Vulnerability,
)


# ── Helpers ──────────────────────────────────────────────────────


def _make_tool_call(call_id, name, arguments):
    """Build a single tool_call dict matching OpenAI format."""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _make_response(tool_calls=None, content=None, prompt_tokens=100, completion_tokens=50):
    """Build a mock inference response matching OpenAI chat completion shape."""
    message = {"role": "assistant"}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if content:
        message["content"] = content
    return {
        "choices": [{"message": message}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


SAMPLE_VULNERABILITIES = [
    {
        "title": "Reentrancy in withdraw",
        "description": "State updated after external call allows reentrancy.",
        "vulnerability_type": "reentrancy",
        "severity": "critical",
        "confidence": 0.95,
        "location": "withdraw(uint256)",
        "file": "src/Vault.sol",
    },
    {
        "title": "Missing access control on init",
        "description": "Anyone can call initialize().",
        "vulnerability_type": "access-control",
        "severity": "high",
        "confidence": 0.8,
        "location": "initialize()",
        "file": "src/Vault.sol",
    },
]


@pytest.fixture
def project_dir():
    """Create a temp project directory with a .sol file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "src"
        src.mkdir()
        (src / "Vault.sol").write_text(
            "// SPDX-License-Identifier: MIT\n"
            "pragma solidity ^0.8.0;\n"
            "contract Vault {\n"
            "    mapping(address => uint256) public balances;\n"
            "    function withdraw(uint256 a) external {\n"
            "        (bool ok,) = msg.sender.call{value: a}('');\n"
            "        require(ok);\n"
            "        balances[msg.sender] -= a;\n"
            "    }\n"
            "}\n"
        )
        yield Path(tmpdir)


@pytest.fixture
def runner():
    """Create a BaselineRunner with a dummy config."""
    config = {"model": "test-model"}
    previous = os.environ.get("INFERENCE_API_KEY")
    os.environ["INFERENCE_API_KEY"] = "cpk_test"
    try:
        return BaselineRunner(config, inference_api="http://fake:8000")
    finally:
        if previous is None:
            os.environ.pop("INFERENCE_API_KEY", None)
        else:
            os.environ["INFERENCE_API_KEY"] = previous


# ── Tests ────────────────────────────────────────────────────────


class TestToolDefinitions:
    """TOOL_DEFINITIONS has correct shape for OpenAI tool-use API."""

    def test_has_three_tools(self):
        assert len(TOOL_DEFINITIONS) == 3

    def test_tool_names(self):
        names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
        assert names == {"list_files", "read_file", "report_vulnerabilities"}

    def test_each_has_type_function(self):
        for t in TOOL_DEFINITIONS:
            assert t["type"] == "function"
            assert "parameters" in t["function"]


class TestToolExecution:
    """_execute_tool_call dispatches to the right handler."""

    def test_list_files(self, runner, project_dir):
        tc = _make_tool_call("c1", "list_files", {"directory": "."})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert "files" in result
        assert "src/" in result["files"]

    def test_list_files_subdirectory(self, runner, project_dir):
        tc = _make_tool_call("c2", "list_files", {"directory": "src"})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert any("Vault.sol" in f for f in result["files"])

    def test_read_file(self, runner, project_dir):
        tc = _make_tool_call("c3", "read_file", {"file_path": "src/Vault.sol"})
        result = runner._execute_tool_call(tc, project_dir)
        assert "contract Vault" in result

    def test_read_file_sanitizes_spacing_glitches(self, runner, project_dir):
        tc = _make_tool_call("c3b", "read_file", {"file_path": "src / Vault. sol"})
        result = runner._execute_tool_call(tc, project_dir)
        assert "contract Vault" in result

    def test_read_file_not_found(self, runner, project_dir):
        tc = _make_tool_call("c4", "read_file", {"file_path": "nope.sol"})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert "error" in result

    def test_read_file_missing_path_returns_error(self, runner, project_dir):
        tc = _make_tool_call("c4b", "read_file", {})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert result["error"] == "Missing or invalid required argument: file_path"

    def test_invalid_tool_argument_json_returns_error(self, runner, project_dir):
        tc = {
            "id": "c4c",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": "{not-json",
            },
        }
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert "Invalid tool arguments JSON" in result["error"]

    def test_path_traversal_blocked_list(self, runner, project_dir):
        tc = _make_tool_call("c5", "list_files", {"directory": "../"})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_path_traversal_blocked_read(self, runner, project_dir):
        tc = _make_tool_call("c6", "read_file", {"file_path": "../../etc/passwd"})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_report_vulnerabilities(self, runner, project_dir):
        args = {"vulnerabilities": SAMPLE_VULNERABILITIES}
        tc = _make_tool_call("c7", "report_vulnerabilities", args)
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert result == args

    def test_unknown_tool(self, runner, project_dir):
        tc = _make_tool_call("c8", "delete_everything", {})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert "error" in result
        assert "Unknown tool" in result["error"]


class TestAnalyzeProjectWithTools:
    """Per-file tool-use loop with seeded target file context."""

    def _build_side_effects(self):
        """Two inference responses simulating one extra read then final report."""
        return [
            # Turn 1: model asks for one additional read
            _make_response(
                tool_calls=[_make_tool_call("tc1", "read_file", {"file_path": "src/Vault.sol"})],
                prompt_tokens=200,
                completion_tokens=30,
            ),
            # Turn 2: model reports vulnerabilities
            _make_response(
                tool_calls=[
                    _make_tool_call("tc2", "report_vulnerabilities", {"vulnerabilities": SAMPLE_VULNERABILITIES}),
                ],
                prompt_tokens=250,
                completion_tokens=25,
            ),
        ]

    @patch.object(BaselineRunner, "inference")
    def test_returns_analysis_result(self, mock_inference, runner, project_dir):
        mock_inference.side_effect = self._build_side_effects()
        result = runner.analyze_project_with_tools(project_dir, "test-project")
        assert isinstance(result, AnalysisResult)

    @patch.object(BaselineRunner, "inference")
    def test_model_explores_with_list_and_read(self, mock_inference, runner, project_dir):
        """Seeded file list is available and the model uses one extra read before reporting."""
        mock_inference.side_effect = self._build_side_effects()
        runner.analyze_project_with_tools(project_dir, "test-project")

        # 2 inference calls: one extra read, then report
        assert mock_inference.call_count == 2

    @patch.object(BaselineRunner, "inference")
    def test_tool_definitions_sent(self, mock_inference, runner, project_dir):
        """Inference requests include tool definitions."""
        mock_inference.side_effect = self._build_side_effects()
        runner.analyze_project_with_tools(project_dir, "test-project")

        for call in mock_inference.call_args_list:
            assert call.kwargs.get("tools") == TOOL_DEFINITIONS

    @patch.object(BaselineRunner, "inference")
    def test_vulnerabilities_parsed(self, mock_inference, runner, project_dir):
        mock_inference.side_effect = self._build_side_effects()
        result = runner.analyze_project_with_tools(project_dir, "test-project")

        assert result.total_vulnerabilities == 2
        assert len(result.vulnerabilities) == 2

    @patch.object(BaselineRunner, "inference")
    def test_vulnerabilities_have_required_fields(self, mock_inference, runner, project_dir):
        """AnalysisResult is scorer-compatible: vulnerabilities have expected fields."""
        mock_inference.side_effect = self._build_side_effects()
        result = runner.analyze_project_with_tools(project_dir, "test-project")

        required_fields = {"title", "description", "vulnerability_type", "severity", "confidence", "location", "file", "id", "reported_by_model"}
        for v in result.vulnerabilities:
            assert isinstance(v, Vulnerability)
            vuln_dict = v.model_dump()
            for field in required_fields:
                assert field in vuln_dict, f"Missing field: {field}"

    @patch.object(BaselineRunner, "inference")
    def test_reported_by_model_set(self, mock_inference, runner, project_dir):
        mock_inference.side_effect = self._build_side_effects()
        result = runner.analyze_project_with_tools(project_dir, "test-project")

        for v in result.vulnerabilities:
            assert v.reported_by_model == "test-model"

    @patch.object(BaselineRunner, "inference")
    def test_token_usage_accumulated(self, mock_inference, runner, project_dir):
        """Token usage is accumulated across all turns."""
        mock_inference.side_effect = self._build_side_effects()
        result = runner.analyze_project_with_tools(project_dir, "test-project")

        assert result.token_usage["total_input"] == 450
        assert result.token_usage["total_output"] == 55

    @patch.object(BaselineRunner, "inference")
    def test_conversation_includes_tool_results(self, mock_inference, runner, project_dir):
        """Tool results are appended to messages for each turn."""
        mock_inference.side_effect = self._build_side_effects()
        runner.analyze_project_with_tools(project_dir, "test-project")

        # The messages list is mutated in-place, so we check the final state
        # as sent into the second inference call.
        def get_messages(call):
            if call.args:
                return call.args[0]
            return call.kwargs["messages"]

        final_messages = get_messages(mock_inference.call_args_list[-1])
        tool_messages = [m for m in final_messages if isinstance(m, dict) and m.get("role") == "tool"]

        # Seeded list_files + seeded read_file + both model-issued tool call results
        assert len(tool_messages) == 4
        tool_call_ids = [m["tool_call_id"] for m in tool_messages]
        assert "seed-read-0-list" in tool_call_ids
        assert "seed-read-0" in tool_call_ids
        assert "tc1" in tool_call_ids
        assert "tc2" in tool_call_ids

    @patch.object(BaselineRunner, "inference")
    def test_first_call_starts_with_seeded_tool_context(self, mock_inference, runner, project_dir):
        """The first pass is seeded with synthetic list_files and read_file tool results."""
        mock_inference.side_effect = [
            _make_response(
                tool_calls=[
                    _make_tool_call("tc1", "report_vulnerabilities", {"vulnerabilities": SAMPLE_VULNERABILITIES}),
                ],
                prompt_tokens=100,
                completion_tokens=20,
            ),
        ]

        runner.analyze_project_with_tools(project_dir, "test-project")

        first_call = mock_inference.call_args_list[0].kwargs
        messages = first_call["messages"]
        assert messages[2]["role"] == "assistant"
        assert messages[2]["tool_calls"][0]["function"]["name"] == "list_files"
        assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == {"directory": "."}
        assert messages[3]["role"] == "tool"
        assert messages[3]["tool_call_id"] == "seed-read-0-list"
        assert "src/" in messages[3]["content"]
        assert messages[4]["role"] == "assistant"
        assert messages[4]["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(messages[4]["tool_calls"][0]["function"]["arguments"]) == {"file_path": "src/Vault.sol"}
        assert messages[5]["role"] == "tool"
        assert messages[5]["tool_call_id"] == "seed-read-0"
        assert "contract Vault" in messages[5]["content"]

    @patch.object(BaselineRunner, "_analyze_target_file_with_tools")
    def test_runs_one_pass_per_discovered_contract_file(self, mock_analyze_target, runner, project_dir):
        """Each discovered contract file gets its own seeded analysis pass."""
        (project_dir / "src" / "Token.sol").write_text("contract Token {}\n")
        mock_analyze_target.side_effect = [
            ({"src/Token.sol"}, [], 100, 20),
            ({ "src/Vault.sol" }, [Vulnerability(**{**SAMPLE_VULNERABILITIES[0], "reported_by_model": "test-model"})], 100, 20),
        ]

        result = runner.analyze_project_with_tools(project_dir, "test-project")

        called_paths = {call.args[1] for call in mock_analyze_target.call_args_list}
        assert called_paths == {"src/Token.sol", "src/Vault.sol"}
        assert result.files_analyzed == 2
        assert result.total_vulnerabilities == 1

    @patch("miner.agent.ThreadPoolExecutor")
    def test_uses_four_workers_for_parallel_file_passes(self, mock_executor_cls, runner, project_dir):
        """Tool-based file passes should fan out with four workers."""
        completed_future = Future()
        completed_future.set_result(({"src/Vault.sol"}, [], 0, 0))
        mock_executor = MagicMock()
        mock_executor.__enter__.return_value = mock_executor
        mock_executor.__exit__.return_value = False
        mock_executor.submit.return_value = completed_future
        mock_executor_cls.return_value = mock_executor

        runner.analyze_project_with_tools(project_dir, "test-project")

        mock_executor_cls.assert_called_once_with(max_workers=MAX_TOOL_PASS_WORKERS)

    def test_discover_contract_files_filters_tests_and_excluded_dirs(self, runner, project_dir):
        """Local discovery should ignore tests and excluded directories."""
        (project_dir / "src" / "Token.sol").write_text("contract Token {}\n")
        tests_dir = project_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "VaultTest.sol").write_text("contract VaultTest {}\n")
        mocks_dir = project_dir / "mocks"
        mocks_dir.mkdir()
        (mocks_dir / "MockVault.sol").write_text("contract MockVault {}\n")

        files = runner._discover_contract_files(project_dir)
        rel_paths = [str(path.relative_to(project_dir)) for path in files]

        assert rel_paths == ["src/Token.sol", "src/Vault.sol"]

    @patch.object(BaselineRunner, "inference")
    def test_stops_after_report(self, mock_inference, runner, project_dir):
        """Loop stops after report_vulnerabilities, doesn't make extra calls."""
        responses = self._build_side_effects()
        # Add an extra response that should never be reached
        responses.append(_make_response(content="This should not be called"))
        mock_inference.side_effect = responses

        runner.analyze_project_with_tools(project_dir, "test-project")
        assert mock_inference.call_count == 2

    @patch.object(BaselineRunner, "inference")
    def test_stops_when_no_tool_calls(self, mock_inference, runner, project_dir):
        """Loop stops if the model responds without tool_calls."""
        mock_inference.side_effect = [
            _make_response(
                tool_calls=[_make_tool_call("tc1", "list_files", {"directory": "."})],
                prompt_tokens=100,
                completion_tokens=20,
            ),
            # Model responds with text only — no tool calls
            _make_response(content="I found no vulnerabilities.", prompt_tokens=150, completion_tokens=40),
        ]

        result = runner.analyze_project_with_tools(project_dir, "test-project")
        assert mock_inference.call_count == 2
        assert result.total_vulnerabilities == 0

    @patch.object(BaselineRunner, "inference")
    @patch("miner.agent.time.monotonic")
    def test_forces_report_when_deadline_reached(self, mock_monotonic, mock_inference, runner, project_dir):
        """Loop forces report_vulnerabilities once the time budget is exhausted."""
        mock_monotonic.side_effect = [
            100.0,
            100.0,
            100.0,
            100.0 + MAX_TOOL_RUNTIME_SECONDS,
        ]
        mock_inference.side_effect = [
            _make_response(
                tool_calls=[_make_tool_call("tc1", "list_files", {"directory": "."})],
                prompt_tokens=100,
                completion_tokens=20,
            ),
            _make_response(
                tool_calls=[
                    _make_tool_call("tc2", "report_vulnerabilities", {"vulnerabilities": SAMPLE_VULNERABILITIES}),
                ],
                prompt_tokens=100,
                completion_tokens=20,
            ),
        ]

        result = runner.analyze_project_with_tools(project_dir, "test-project")

        assert mock_inference.call_count == 2
        assert result.total_vulnerabilities == 2
        forced_call = mock_inference.call_args_list[-1].kwargs
        assert forced_call["tool_choice"] == {"type": "function", "function": {"name": "report_vulnerabilities"}}
        timeout_messages = [
            message for message in forced_call["messages"]
            if message.get("role") == "user" and "running out of time" in message.get("content", "")
        ]
        assert len(timeout_messages) == 1

    @patch.object(BaselineRunner, "inference")
    @patch("miner.agent.time.monotonic")
    def test_turns_are_capped_per_file_pass(self, mock_monotonic, mock_inference, runner, project_dir):
        """A file pass forces reporting by the second turn."""
        mock_monotonic.side_effect = [100.0, 100.0, 100.0, 101.0]
        mock_inference.side_effect = [
            _make_response(
                tool_calls=[_make_tool_call("tc1", "list_files", {"directory": "."})],
                prompt_tokens=100,
                completion_tokens=20,
            ),
            _make_response(
                tool_calls=[
                    _make_tool_call("tc2", "report_vulnerabilities", {"vulnerabilities": SAMPLE_VULNERABILITIES}),
                ],
                prompt_tokens=100,
                completion_tokens=20,
            ),
        ]

        result = runner.analyze_project_with_tools(project_dir, "test-project")

        assert mock_inference.call_count == 2
        assert result.total_vulnerabilities == 2

    @patch.object(BaselineRunner, "inference")
    @patch("miner.agent.time.monotonic")
    def test_forced_report_failure_exits_cleanly(self, mock_monotonic, mock_inference, runner, project_dir):
        """Loop exits after a forced report attempt that does not actually report."""
        mock_monotonic.side_effect = [
            100.0,
            100.0,
            100.0,
            100.0 + MAX_TOOL_RUNTIME_SECONDS,
        ]
        mock_inference.side_effect = [
            _make_response(
                tool_calls=[_make_tool_call("tc1", "list_files", {"directory": "."})],
                prompt_tokens=100,
                completion_tokens=20,
            ),
            _make_response(
                tool_calls=[_make_tool_call("tc2", "list_files", {"directory": "."})],
                prompt_tokens=100,
                completion_tokens=20,
            ),
        ]

        result = runner.analyze_project_with_tools(project_dir, "test-project")

        assert mock_inference.call_count == 2
        assert result.total_vulnerabilities == 0

    @patch.object(BaselineRunner, "inference")
    def test_deduplicates_vulnerabilities(self, mock_inference, runner, project_dir):
        """Duplicate vulnerabilities (same id) are deduplicated."""
        duped = SAMPLE_VULNERABILITIES + SAMPLE_VULNERABILITIES  # same vulns twice
        mock_inference.side_effect = [
            _make_response(
                tool_calls=[_make_tool_call("tc1", "report_vulnerabilities", {"vulnerabilities": duped})],
                prompt_tokens=100,
                completion_tokens=100,
            ),
        ]
        result = runner.analyze_project_with_tools(project_dir, "test-project")
        assert result.total_vulnerabilities == 2  # deduplicated

    @patch.object(BaselineRunner, "inference")
    def test_result_is_json_serializable(self, mock_inference, runner, project_dir):
        """AnalysisResult can be serialized to JSON (scorer compatibility)."""
        mock_inference.side_effect = self._build_side_effects()
        result = runner.analyze_project_with_tools(project_dir, "test-project")

        result_dict = result.model_dump(mode="json")
        serialized = json.dumps(result_dict)
        assert serialized  # no exception
        parsed = json.loads(serialized)
        assert parsed["total_vulnerabilities"] == 2
        assert len(parsed["vulnerabilities"]) == 2


class TestInferenceKwargs:
    """inference() passes through extra kwargs to the payload."""

    @patch("miner.agent.requests.post")
    def test_tools_in_payload(self, mock_post, runner):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        runner.inference(
            [{"role": "user", "content": "hi"}],
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
        )

        headers = mock_post.call_args.kwargs["headers"]
        payload = mock_post.call_args.kwargs["json"]
        assert headers["x-inference-api-key"] == "cpk_test"
        assert "provider" not in payload
        assert payload["tools"] == TOOL_DEFINITIONS
        assert payload["tool_choice"] == "auto"

    @patch("miner.agent.requests.post")
    def test_response_format_override(self, mock_post, runner):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        runner.inference(
            [{"role": "user", "content": "hi"}],
            response_format={"type": "text"},
        )

        payload = mock_post.call_args.kwargs["json"]
        assert payload["response_format"] == {"type": "text"}
