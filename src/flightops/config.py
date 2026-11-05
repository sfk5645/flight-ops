"""Settings read from environment variables (or a local .env file)."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class R2Settings(BaseSettings):
    """Cloudflare R2 connection settings, read from R2_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="R2_", env_file=".env", extra="ignore")

    account_id: str
    access_key_id: str
    secret_access_key: SecretStr
    bucket: str = "flight-ops-lake"

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"
