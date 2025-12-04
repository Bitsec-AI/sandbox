from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str | None = "development"
    local: bool = False
    wallet_name: str | None = None

    chutes_api_key: str | None

    app_url: str = "bitsec.ai"
    platform_url: str = "bitsec.ai"

    host_cwd: str = "."
    validator_dir: str = "validator"

    proxy_container: str = "bitsec_proxy"
    proxy_network: str = "bitsec-net"
    proxy_port: int = 8087
    proxy_url: str = "http://localhost:8087"

    skip_execution: bool = False
    skip_evaluation: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow"
    )

settings = Settings()
