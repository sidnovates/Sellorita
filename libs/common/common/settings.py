"""All runtime configuration, read from environment variables (12-factor config).

Locally the values come from a gitignored `.env` file; in Kubernetes the Vault Agent
injects the secrets into the pod environment. The application never talks to Vault.

Secrets are typed as `SecretStr` so they print as '**********' if a Settings object is
ever logged, and connection URLs are plain properties (not fields) so they are never
included in `model_dump()`.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The repo-root .env, also when a process runs from services/<name>/ (e.g.
        # `celery --workdir services/agent_worker`). Later files win. Missing files are
        # ignored, so containers (no .env, real env vars) are unaffected.
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ---------------------------------------------------------------------
    app_env: Literal["local", "test", "staging", "prod"] = "local"
    service_name: str = "sellorita"
    log_level: str = "INFO"
    log_json: bool = True  # False gives coloured, human-readable logs for local work
    mock_mode: bool = True  # True: no real LLM/news/render calls (CI, local dev)

    # --- PostgreSQL ------------------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "sellorita_db"
    postgres_user: str = "sellorita"
    postgres_password: SecretStr = SecretStr("")
    # Without a timeout, a connect to an unreachable host can hang for minutes, and a
    # readiness probe or a pytest run would hang with it.
    db_connect_timeout_seconds: int = 5

    # --- Redis -----------------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: SecretStr = SecretStr("")
    redis_cache_db: int = 2  # intelligence cache; tests use a separate DB

    # --- MinIO (S3-compatible object storage) ----------------------------------------
    # Presigned URL signatures include the host, so the worker (inside the cluster) and
    # the browser (outside it) need URLs signed for different endpoints.
    minio_internal_endpoint: str = "http://localhost:9000"
    minio_public_endpoint: str = "http://localhost:9000"
    minio_root_user: SecretStr = SecretStr("")
    minio_root_password: SecretStr = SecretStr("")
    minio_bucket: str = "sellorita-media"
    minio_region: str = "us-east-1"
    presigned_url_expiry_seconds: int = 3600

    # --- External APIs ---------------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    news_api_key: SecretStr = SecretStr("")
    llm_model_fast: str = "claude-haiku-5-5"  # intelligence + copy agents
    llm_model_smart: str = "claude-sonnet-5-5"  # creative director, HTML authoring

    # --- Pipeline behaviour ----------------------------------------------------------
    celery_queue: str = "campaigns"
    qa_max_retries: int = 2
    intel_cache_ttl_seconds: int = 43200  # 12 hours
    render_scratch_dir: str = Field(default_factory=tempfile.gettempdir)
    ffmpeg_binary: str = "ffmpeg"
    schedule_check_interval_seconds: int = 900  # Celery Beat: schedule_due_campaigns

    # --- Mock-mode knobs (only used when MOCK_MODE=true) ------------------------------
    mock_reel_seconds: int = 3  # length of the dummy MP4 (real FFmpeg encode)
    # Make the QA critic reject the first N attempts, to demo/test the retry loop and
    # the FAILED path. 0 = always pass; > QA_MAX_RETRIES = campaign ends FAILED.
    mock_qa_failures: int = 0

    # --- Derived connection URLs -----------------------------------------------------
    @property
    def database_url(self) -> str:
        """Async URL for the API gateway (SQLAlchemy + asyncpg)."""
        return self._postgres_url("postgresql+asyncpg")

    @property
    def database_url_sync(self) -> str:
        """Sync URL for Celery workers and Alembic (SQLAlchemy + psycopg)."""
        return self._postgres_url("postgresql+psycopg")

    @property
    def celery_broker_url(self) -> str:
        return self._redis_url(db=0)

    @property
    def celery_result_backend(self) -> str:
        return self._redis_url(db=1)

    @property
    def redis_cache_url(self) -> str:
        return self._redis_url(db=self.redis_cache_db)

    def _postgres_url(self, driver: str) -> str:
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password.get_secret_value())
        return (
            f"{driver}://{user}:{password}@{self.postgres_host}:{self.postgres_port}"
            f"/{self.postgres_db}"
        )

    def _redis_url(self, db: int) -> str:
        password = self.redis_password.get_secret_value()
        auth = f":{quote_plus(password)}@" if password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{db}"

    # --- Fail fast on missing configuration -------------------------------------------
    @model_validator(mode="after")
    def _check_required_secrets(self) -> Settings:
        missing: list[str] = []
        if not self.mock_mode and not self.anthropic_api_key.get_secret_value():
            missing.append("ANTHROPIC_API_KEY (required when MOCK_MODE=false)")
        if self.app_env in ("staging", "prod"):
            for name in ("postgres_password", "minio_root_user", "minio_root_password"):
                if not getattr(self, name).get_secret_value():
                    missing.append(name.upper())
        if missing:
            raise ValueError("Missing required configuration: " + ", ".join(missing))
        return self


@lru_cache
def get_settings() -> Settings:
    """Settings are read once per process and then reused."""
    return Settings()
