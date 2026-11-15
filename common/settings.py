"""Environment settings (.env) and runtime config (config/hanlane.toml)."""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    mongodb_uri: str = ""
    mongodb_db: str = "hanlane"
    dd_env: str = "hackathon"
    dd_agent_host: str = "localhost"
    dd_dogstatsd_port: int = 8125
    dd_api_key: str = ""
    dd_app_key: str = ""
    dd_site: str = "ap1.datadoghq.com"
    gateway_url: str = "http://localhost:8000"
    sim_control_url: str = "http://localhost:8100"
    worker_url: str = "http://localhost:8200"
    write_mode: str = "batch"
    map_style_url: str = ""
    map_api_key: str = ""
    hanlane_config: str = str(ROOT / "config" / "hanlane.toml")


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def config() -> dict[str, Any]:
    with open(settings().hanlane_config, "rb") as f:
        return tomllib.load(f)


def path(rel: str) -> Path:
    """Resolve a repo-relative path from the config."""
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p
