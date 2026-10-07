"""
config.settings
===============

Centralized configuration for WayPoint.
"""
from __future__ import annotations

import secrets
import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WAYPOINT_VERSION = "2.1.0"

_WEAK_PASSWORDS = {
    "incident_pass", "neo4j_pass", "password", "pass", "secret", "changeme",
    "change-me", "change_me", "change-me-to-a-long-random-secret-at-least-32-chars",
    "change_me_strong_password", "admin", "root", "neo4j", "postgres", "test",
    "123456", "password123",
}

# Used only where no PRISM_JWT_SECRET is configured AND the environment is
# development/test (every other environment refuses to start). Random per
# process, so nothing is forgeable and nothing is shared between deployments.
_EPHEMERAL_JWT_SECRET = secrets.token_urlsafe(48)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Without this, a field declared with an alias (e.g. jwt_secret /
        # PRISM_JWT_SECRET) can only be set through the alias: passing the
        # Python name to Settings(...) is silently dropped rather than
        # raising, so a caller gets an empty secret and no error.
        populate_by_name=True,
        extra="ignore",
    )

    database_url: str = Field(default="postgresql+asyncpg://incident:incident_pass@localhost:5432/incident_db")
    database_sync_url: str = Field(default="postgresql+psycopg2://incident:incident_pass@localhost:5432/incident_db")
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j_pass"

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_embed_model: str = "nomic-embed-text"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_request_timeout: float = 60.0
    # Used only to derive the Fernet key for tenant LLM credentials. In non-local
    # environments it must be set explicitly alongside PRISM_JWT_SECRET.
    llm_credential_secret: str = Field(default="", alias="WAYPOINT_LLM_CREDENTIAL_SECRET")

    sentence_transformer_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
    faiss_index_path: str = str(PROJECT_ROOT / "data" / "faiss_index")

    app_env: str = "development"
    app_log_level: str = "INFO"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    api_key: Optional[str] = Field(default=None)
    api_key_min_length: int = 32
    cors_origins: str = Field(default="")

    # Session/credential material. These are declared here rather than read via
    # ``os.getenv`` at their point of use: the settings loader reads ``.env``
    # into this object and never mutates ``os.environ``, so a secret supplied
    # only in ``.env`` would otherwise be invisible.
    jwt_secret: str = Field(default="", validation_alias=AliasChoices("WAYPOINT_JWT_SECRET", "PRISM_JWT_SECRET"))
    jwt_secret_min_length: int = 32
    session_hours: int = Field(default=8, validation_alias=AliasChoices("WAYPOINT_SESSION_HOURS", "PRISM_SESSION_HOURS"))
    bootstrap_email: str = Field(default="", validation_alias=AliasChoices("WAYPOINT_BOOTSTRAP_EMAIL", "PRISM_BOOTSTRAP_EMAIL"))
    bootstrap_password: str = Field(default="", validation_alias=AliasChoices("WAYPOINT_BOOTSTRAP_PASSWORD", "PRISM_BOOTSTRAP_PASSWORD"))
    bootstrap_tenant_name: str = Field(default="WayPoint Workspace", validation_alias=AliasChoices("WAYPOINT_BOOTSTRAP_TENANT_NAME", "PRISM_BOOTSTRAP_TENANT_NAME"))
    password_min_length: int = 12

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_async_database_url(cls, v: str) -> str:
        # Render exposes PostgreSQL URLs with the generic postgres scheme.
        # SQLAlchemy async engines need the asyncpg driver explicitly.
        value = str(v or "")
        if value.startswith("postgres://"):
            return "postgresql+asyncpg://" + value[len("postgres://"):]
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value[len("postgresql://"):]
        return value

    @field_validator("database_sync_url", mode="before")
    @classmethod
    def _normalize_sync_database_url(cls, v: str) -> str:
        # Alembic uses a synchronous SQLAlchemy engine for migrations.
        value = str(v or "")
        if value.startswith("postgres://"):
            return "postgresql+psycopg2://" + value[len("postgres://"):]
        if value.startswith("postgresql://"):
            return "postgresql+psycopg2://" + value[len("postgresql://"):]
        return value

    max_concurrent_investigations: int = 3
    investigation_rate_limit: str = "10/minute"
    # Tenant context is always enforced; the flag is retained only so existing
    # deployments fail loudly rather than silently reopening the boundary.
    require_tenant_header: bool = True
    max_service_accounts_per_tenant: int = 25
    redis_url: str = ""
    max_affected_services: int = 50
    max_log_lines: int = 500
    max_log_line_chars: int = 2000
    max_traces: int = 200
    max_request_body_bytes: int = 1_048_576
    max_memory_query_length: int = 2000
    max_pagination_limit: int = 100
    max_prediction_batch: int = 50

    run_migrations_on_startup: bool = False
    job_worker_enabled: bool = False
    job_poll_interval: float = 1.0
    job_attempts_max: int = 3
    job_lease_seconds: float = 300.0

    MDV_THRESHOLD: float = Field(default=0.15, description="MDV stop threshold", env="PRISM_MDV_THRESHOLD")
    MAX_INVESTIGATION_STEPS: int = Field(default=20, description="Maximum investigation iterations", env="PRISM_MAX_INVESTIGATION_STEPS")

    agent_default_reliability: float = 0.5
    agent_min_reliability: float = 0.2
    agent_reliability_decay: float = 0.9
    agent_timeout_seconds: float = 45.0

    learning_mode: str = Field(default="online", min_length=1)
    learning_require_confirmation: bool = Field(default=True)
    experiment_logging: bool = Field(default=False, validation_alias=AliasChoices("WAYPOINT_EXPERIMENT_LOGGING", "PRISM_EXPERIMENT_LOGGING"))

    consensus_min_voters: int = 2
    consensus_confidence_threshold: float = 0.6
    causal_max_depth: int = 10
    causal_min_confidence: float = 0.1
    propagation_iterations: int = 5
    # When consensus is undetermined, promote the highest-confidence graph
    # traversal candidate. Previously both call sites hardcoded False, which
    # left apply_graph_traversal_fallback permanently dead code.
    graph_traversal_fallback: bool = False
    memory_top_k: int = 5
    memory_similarity_threshold: float = 0.5

    enable_neo4j: bool = True
    enable_ollama: bool = True
    enable_faiss: bool = True

    @field_validator("app_log_level")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator("learning_mode")
    @classmethod
    def _validate_learning_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in {"online", "frozen"}:
            raise ValueError("learning_mode must be 'online' or 'frozen'")
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env.lower() in {"test", "testing"}

    @property
    def is_local(self) -> bool:
        """Environments where relaxed credential rules are acceptable.

        Anything that is not an explicitly local environment -- including
        unknown values such as ``staging``, ``preview`` or a typo -- is
        validated. Validating only on the literal string ``production`` let
        misconfigured deploy targets start with placeholder secrets.
        """
        return self.app_env.lower() in {"development", "dev", "local", "test", "testing"}

    @property
    def data_dir(self) -> Path:
        if self.is_test:
            import tempfile
            path = Path(tempfile.gettempdir()) / "prism_test_data"
        else:
            path = PROJECT_ROOT / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cors_origins_list(self) -> List[str]:
        raw = (self.cors_origins or "").strip()
        if raw:
            return [o.strip() for o in raw.split(",") if o.strip()]
        if self.is_production:
            return []
        return ["http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:3000", "http://127.0.0.1:8000"]

    @staticmethod
    def _url_password(url: str) -> Optional[str]:
        """Return only the URL password, never the whole URL.

        Checking the complete URL is incorrect because schemes such as
        postgresql:// naturally contain words that may also be weak-password
        tokens.
        """
        try:
            parsed = urlsplit(url)
            if parsed.password is None:
                return None
            return unquote(parsed.password)
        except ValueError:
            return None

    @staticmethod
    def _is_weak_secret(value: Optional[str]) -> bool:
        if not value:
            return True
        return value.strip().lower() in _WEAK_PASSWORDS

    @property
    def signing_secret(self) -> str:
        """The key actually used to sign and verify session JWTs.

        Non-local environments are validated at startup, so this always returns
        the configured secret there. In development/test, an unset secret yields
        a random per-process key instead of an empty string: signing with ``""``
        raises ``InvalidKeyError: HMAC key must not be empty`` deep inside PyJWT,
        and any hardcoded fallback would silently make every deployment share a
        publicly known signing key.
        """
        if self.jwt_secret:
            return self.jwt_secret
        return _EPHEMERAL_JWT_SECRET

    def validate_production_secrets(self) -> None:
        if self.is_local:
            return

        errors: List[str] = []
        if not self.jwt_secret:
            errors.append("PRISM_JWT_SECRET is not configured")
        elif len(self.jwt_secret) < self.jwt_secret_min_length:
            errors.append(
                f"PRISM_JWT_SECRET is too short (min {self.jwt_secret_min_length} characters)"
            )
        elif self._is_weak_secret(self.jwt_secret):
            errors.append("PRISM_JWT_SECRET is still set to a placeholder/default value")

        for name, url in (("DATABASE_URL", self.database_url), ("DATABASE_SYNC_URL", self.database_sync_url)):
            password = self._url_password(url)
            if password is None:
                errors.append(f"{name} must contain an explicit password in production")
            elif self._is_weak_secret(password):
                errors.append(f"{name} contains a weak/default password")

        if self.enable_neo4j and self._is_weak_secret(self.neo4j_password):
            errors.append("NEO4J_PASSWORD is weak/default")
        if not self.cors_origins_list:
            errors.append("CORS_ORIGINS must be set to explicit frontend origin(s) in production")
        if not self.redis_url:
            errors.append("REDIS_URL is required in non-local environments for shared rate limiting and JWT revocation")

        if errors:
            print(
                "WayPoint refused to start in production due to insecure configuration:\n  - " + "\n  - ".join(errors),
                file=sys.stderr,
            )
            raise SystemExit(1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.validate_production_secrets()
    return s


settings = get_settings()
