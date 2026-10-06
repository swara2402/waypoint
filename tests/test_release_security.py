from __future__ import annotations

from config.settings import Settings


def test_postgresql_scheme_is_not_treated_as_a_weak_password() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://incident:strong-random-password-123@db:5432/incident_db",
        database_sync_url="postgresql+psycopg2://incident:strong-random-password-123@db:5432/incident_db",
        neo4j_password="another-strong-secret",
        jwt_secret="jwt-secret-that-is-long-enough-to-pass-32-chars",
        api_key="x" * 40,
        cors_origins="https://waypoint.example.com",
        redis_url="redis://localhost:6379/0",
    )
    settings.validate_production_secrets()


def test_weak_database_password_is_rejected() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://incident:postgres@db:5432/incident_db",
        database_sync_url="postgresql+psycopg2://incident:postgres@db:5432/incident_db",
        neo4j_password="another-strong-secret",
        jwt_secret="jwt-secret-that-is-long-enough-to-pass-32-chars",
        api_key="x" * 40,
        cors_origins="https://waypoint.example.com",
    )
    try:
        settings.validate_production_secrets()
    except SystemExit:
        return
    raise AssertionError("weak database password must fail production validation")
