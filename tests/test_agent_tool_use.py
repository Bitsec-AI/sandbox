"""Tests for the tool-use agent loop in miner/agent.py.

Mocks the inference API to simulate a multi-turn conversation where the model
calls list_files, read_file, and report_vulnerabilities in sequence.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from miner.agent import (
    BaselineRunner,
    AnalysisResult,
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
    return BaselineRunner(config, inference_api="http://fake:8000")


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

    def test_read_file_not_found(self, runner, project_dir):
        tc = _make_tool_call("c4", "read_file", {"file_path": "nope.sol"})
        result = json.loads(runner._execute_tool_call(tc, project_dir))
        assert "error" in result

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
    """Full tool-use loop: list_files → read_file → report_vulnerabilities."""

    def _build_side_effects(self):
        """Three inference responses simulating a realistic conversation."""
        return [
            # Turn 1: model calls list_files
            _make_response(
                tool_calls=[_make_tool_call("tc1", "list_files", {"directory": "."})],
                prompt_tokens=200,
                completion_tokens=30,
            ),
            # Turn 2: model calls read_file
            _make_response(
                tool_calls=[_make_tool_call("tc2", "read_file", {"file_path": "src/Vault.sol"})],
                prompt_tokens=250,
                completion_tokens=25,
            ),
            # Turn 3: model calls report_vulnerabilities
            _make_response(
                tool_calls=[
                    _make_tool_call("tc3", "report_vulnerabilities", {"vulnerabilities": SAMPLE_VULNERABILITIES}),
                ],
                prompt_tokens=500,
                completion_tokens=200,
            ),
        ]

    @patch.object(BaselineRunner, "inference")
    def test_returns_analysis_result(self, mock_inference, runner, project_dir):
        mock_inference.side_effect = self._build_side_effects()
        result = runner.analyze_project_with_tools(project_dir, "test-project")
        assert isinstance(result, AnalysisResult)

    @patch.object(BaselineRunner, "inference")
    def test_model_explores_with_list_and_read(self, mock_inference, runner, project_dir):
        """Model uses list_files and read_file before reporting."""
        mock_inference.side_effect = self._build_side_effects()
        runner.analyze_project_with_tools(project_dir, "test-project")

        # 3 inference calls: list_files, read_file, report
        assert mock_inference.call_count == 3

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

        # 200 + 250 + 500 = 950 input tokens
        assert result.token_usage["total_input"] == 950
        # 30 + 25 + 200 = 255 output tokens
        assert result.token_usage["total_output"] == 255

    @patch.object(BaselineRunner, "inference")
    def test_conversation_includes_tool_results(self, mock_inference, runner, project_dir):
        """Tool results are appended to messages for each turn."""
        mock_inference.side_effect = self._build_side_effects()
        runner.analyze_project_with_tools(project_dir, "test-project")

        # The messages list is mutated in-place, so we check the final state
        # which should contain all tool results from all 3 turns
        def get_messages(call):
            if call.args:
                return call.args[0]
            return call.kwargs["messages"]

        final_messages = get_messages(mock_inference.call_args_list[-1])
        tool_messages = [m for m in final_messages if isinstance(m, dict) and m.get("role") == "tool"]

        # 3 tool calls = 3 tool result messages (list_files, read_file, report_vulnerabilities)
        assert len(tool_messages) == 3
        tool_call_ids = [m["tool_call_id"] for m in tool_messages]
        assert "tc1" in tool_call_ids
        assert "tc2" in tool_call_ids
        assert "tc3" in tool_call_ids

    @patch.object(BaselineRunner, "inference")
    def test_stops_after_report(self, mock_inference, runner, project_dir):
        """Loop stops after report_vulnerabilities, doesn't make extra calls."""
        responses = self._build_side_effects()
        # Add an extra response that should never be reached
        responses.append(_make_response(content="This should not be called"))
        mock_inference.side_effect = responses

        runner.analyze_project_with_tools(project_dir, "test-project")
        assert mock_inference.call_count == 3

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
    def test_max_turns_respected(self, mock_inference, runner, project_dir):
        """Loop stops after MAX_TOOL_TURNS even if model keeps calling tools."""
        # Return list_files calls forever
        mock_inference.side_effect = [
            _make_response(
                tool_calls=[_make_tool_call(f"tc{i}", "list_files", {"directory": "."})],
                prompt_tokens=100,
                completion_tokens=20,
            )
            for i in range(20)
        ]

        from miner.agent import MAX_TOOL_TURNS
        result = runner.analyze_project_with_tools(project_dir, "test-project")
        assert mock_inference.call_count == MAX_TOOL_TURNS

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

        payload = mock_post.call_args.kwargs["json"]
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
