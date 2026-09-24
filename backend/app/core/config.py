from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


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
    clamav_host: str | None = None
    clamav_port: int = 3310

    @model_validator(mode="after")
    def reject_insecure_shared_configuration(self):
        if self.app_env.lower() in {"local", "development", "test"}:
            return self
        secret = self.jwt_secret_key.get_secret_value()
        if len(secret) < 32 or secret.startswith("REPLACE_") or secret in {
            "local-development-only-change-me",
            "please-change-this-before-shared-testing",
        }:
            raise ValueError("JWT_SECRET_KEY must be a unique secret of at least 32 characters")
        database = make_url(self.database_url)
        if (
            database.get_backend_name() != "postgresql"
            or not database.password
            or len(database.password) < 16
            or database.password == "zhituo_dev"
            or database.password.startswith("REPLACE_")
        ):
            raise ValueError(
                "Shared environments require PostgreSQL and a strong database password"
            )
        if self.debug or self.bootstrap_demo_users:
            raise ValueError(
                "DEBUG and BOOTSTRAP_DEMO_USERS must be false outside local development"
            )
        if self.bootstrap_admin_password and self.bootstrap_admin_password.get_secret_value():
            raise ValueError("BOOTSTRAP_ADMIN_PASSWORD must be empty outside local development")
        if (
            self.bootstrap_demo_user_password
            and self.bootstrap_demo_user_password.get_secret_value()
        ):
            raise ValueError("BOOTSTRAP_DEMO_USER_PASSWORD must be empty outside local development")
        if not self.clamav_host:
            raise ValueError(
                "Shared environments require CLAMAV_HOST for fail-closed file scanning"
            )
        if not self.refresh_cookie_secure:
            raise ValueError("Shared environments require HTTPS and REFRESH_COOKIE_SECURE=true")
        if "*" in self.trusted_host_list or "*" in self.cors_origin_list:
            raise ValueError("Shared environments must restrict trusted hosts and CORS")
        return self

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
