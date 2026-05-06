from base_client import BaseProviderClient


class OpenRouterClient(BaseProviderClient):
    @property
    def provider_name(self) -> str:
        return "openrouter"

    @property
    def api_url(self) -> str:
        return "https://openrouter.ai/api/v1/chat/completions"

    @property
    def default_model(self) -> str:
        return "openai/gpt-4.1-mini"

    def build_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://bitsec.ai",
            "X-Title": "Bitsec",
        }
