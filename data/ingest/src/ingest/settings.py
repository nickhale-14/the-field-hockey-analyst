from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_prefix="",
    )

    # Optional for `ingest validate`; required for `migrate` / `run`.
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    ingest_source: Literal["eh_api"] = Field(
        default="eh_api", alias="INGEST_SOURCE"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # England Hockey API
    eh_api_base_url: str = Field(
        default="https://ehdwapi.englandhockey.co.uk/api", alias="EH_API_BASE_URL"
    )
    eh_api_key: str | None = Field(default=None, alias="EH_API_KEY")
    eh_season_id: str | None = Field(default=None, alias="EH_SEASON_ID")
    # Separate from eh_season_id on purpose - used only by the live fixture/score
    # tooling (sources/eh_api_fixtures.py), kept independent of the historic importer.
    eh_live_season_id: str | None = Field(default=None, alias="EH_LIVE_SEASON_ID")
    eh_competition_id: str | None = Field(default=None, alias="EH_COMPETITION_ID")
    eh_page_size: int = Field(default=100, alias="EH_PAGE_SIZE")
    eh_fetch_fixture_details: bool = Field(
        default=False, alias="EH_FETCH_FIXTURE_DETAILS"
    )
    eh_request_timeout_seconds: int = Field(
        default=30, alias="EH_REQUEST_TIMEOUT_SECONDS"
    )
