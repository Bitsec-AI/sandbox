from base_client import BaseProviderClient


class ChutesClient(BaseProviderClient):
    @property
    def provider_name(self) -> str:
        return "chutes"

    @property
    def api_url(self) -> str:
        return "https://llm.chutes.ai/v1/chat/completions"

    @property
    def default_model(self) -> str:
        return "Qwen/Qwen3-32B-TEE"

    def build_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "X-Identifier": "Bitsec",
        }
