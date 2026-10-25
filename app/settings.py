from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOTUNE_")
    database_url: SecretStr
    collector_database_url: SecretStr | None = None
    api_key: SecretStr = Field(min_length=1)
    sources_path: str = "config/sources.yaml"
    policy_path: str | None = None
    poll_seconds: float = 2

    @property
    def collector_dsn(self):
        return (self.collector_database_url or self.database_url).get_secret_value()
