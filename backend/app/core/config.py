from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "智拓商机作战助手 API"
    app_env: str = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    frontend_dir: str | None = None

    database_url: str = "postgresql+psycopg://zhituo:zhituo_dev@127.0.0.1:5432/zhituo"
    redis_url: str = "redis://127.0.0.1:6379/0"
    cors_origins: str = "http://127.0.0.1:8766,http://localhost:8766"
    trusted_hosts: str = "127.0.0.1,localhost"

    jwt_secret_key: SecretStr = SecretStr("local-development-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    refresh_cookie_secure: bool = False

    bootstrap_admin_username: str = "groupadmin"
    bootstrap_admin_employee_no: str = "CMCC0001"
    bootstrap_admin_display_name: str = "集团管理员"
    bootstrap_admin_password: SecretStr | None = SecretStr("szyd123456")
    bootstrap_demo_users: bool = True
    bootstrap_demo_user_password: SecretStr | None = SecretStr("szyd123456")

    qcc_app_key: SecretStr | None = None
    qcc_secret_key: SecretStr | None = None
    tencent_map_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    qwen_api_key: SecretStr | None = None
    glm_api_key: SecretStr | None = None
    kimi_api_key: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
