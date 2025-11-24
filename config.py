from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str | None = "development"
    local: bool = False
    wallet_name: str | None = None

    chutes_api_key: str | None
    openai_api_key: str | None

    app_url: str = "bitsec.ai"
    platform_url: str = "bitsec.ai"

    host_cwd: str = "."

    proxy_port: int = 8087
    skip_execution: bool = False
    skip_evaluation: bool = False

    model_config = SettingsConfigDict(
        env_file=f".env",
        env_file_encoding="utf-8",
        extra="allow"
    )

settings = Settings()
