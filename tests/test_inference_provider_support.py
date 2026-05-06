from unittest.mock import MagicMock, patch

import pytest

from miner.agent import BaselineRunner
from validator.proxy.models import InferenceProvider, derive_provider_from_api_key


def test_derive_provider_from_api_key_supports_known_prefixes():
    assert derive_provider_from_api_key("cpk_test") == "chutes"
    assert derive_provider_from_api_key("sk-or-test") == "openrouter"


def test_derive_provider_from_api_key_rejects_unknown_prefix():
    with pytest.raises(ValueError):
        derive_provider_from_api_key("bad-key")


def test_provider_model_derives_name_from_api_key():
    provider = InferenceProvider(api_key="sk-or-test")
    assert provider.name == "openrouter"


def test_provider_model_rejects_mismatched_name():
    with pytest.raises(ValueError):
        InferenceProvider(api_key="cpk_test", name="openrouter")


@patch.dict("os.environ", {}, clear=True)
def test_runner_requires_inference_api_key_during_init():
    with pytest.raises(ValueError, match="An inference API key is required."):
        BaselineRunner({"model": "test-model"}, inference_api="http://fake:8000")


@patch("miner.agent.requests.post")
@patch.dict(
    "os.environ",
    {
        "INFERENCE_API_KEY": "sk-or-test",
    },
    clear=False,
)
def test_runner_sends_inference_api_key_header(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"choices": [{"message": {"content": "{}"}}], "usage": {}}
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    runner = BaselineRunner({"model": "test-model"}, inference_api="http://fake:8000")
    runner.inference([{"role": "user", "content": "hi"}])

    headers = mock_post.call_args.kwargs["headers"]
    payload = mock_post.call_args.kwargs["json"]
    assert headers["x-inference-api-key"] == "sk-or-test"
    assert "provider" not in payload
