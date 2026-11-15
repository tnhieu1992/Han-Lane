"""DogStatsD client with the hanlane. prefix. Never tag by vid."""
from __future__ import annotations

from functools import lru_cache

from datadog.dogstatsd import DogStatsd

from common.settings import settings


@lru_cache
def statsd() -> DogStatsd:
    s = settings()
    return DogStatsd(
        host=s.dd_agent_host,
        port=s.dd_dogstatsd_port,
        namespace="hanlane",
        constant_tags=[f"env:{s.dd_env}"],
        disable_telemetry=True,
    )
