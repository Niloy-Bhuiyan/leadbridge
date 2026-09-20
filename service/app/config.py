"""Runtime configuration.

Every integration is optional. A missing credential disables that integration
and is reported by /ops/availability -- it never raises at import time and
never fails silently. Same rule the ingest layer in Shuru follows: a
misconfigured source must be visible, not invisible.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Both locations are searched, later entries winning. Under Docker the
    # working directory is the service root; run natively, uvicorn is
    # started from service/ while .env sits beside docker-compose.yml one
    # level up. Looking in one place only breaks whichever path is not the
    # one the author happened to test.
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- service ----------------------------------------------------------
    app_name: str = "leadbridge"
    environment: Literal["local", "ci", "production"] = "local"
    database_path: str = "leadbridge.db"

    # Shared secret n8n sends as X-Leadbridge-Secret. Empty disables the
    # check, which is only acceptable on a loopback-only local run.
    ingest_secret: str = ""

    # --- LLM provider -----------------------------------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_version: str = "2023-06-01"
    llm_provider: Literal["auto", "anthropic", "mock"] = "auto"

    # --- CRM --------------------------------------------------------------
    hubspot_token: str = ""
    hubspot_base_url: str = "https://api.hubapi.com"
    crm_provider: Literal["auto", "hubspot", "mock"] = "auto"

    # --- SEO --------------------------------------------------------------
    # PageSpeed Insights answers unauthenticated at a low rate limit, so the
    # key is genuinely optional rather than nominally optional.
    pagespeed_api_key: str = ""
    pagespeed_base_url: str = (
        "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    )
    seo_user_agent: str = "leadbridge-audit/0.1 (+https://github.com/Niloy-Bhuiyan)"
    seo_fetch_timeout: float = 10.0
    seo_max_sitemap_urls: int = 200

    # --- outbound HTTP ----------------------------------------------------
    http_max_attempts: int = 3
    http_backoff_base: float = 0.25
    http_backoff_cap: float = 4.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
