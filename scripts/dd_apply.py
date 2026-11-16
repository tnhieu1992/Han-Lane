"""Apply Datadog config from datadog/ (the repo is the source of truth; never edit in the UI).

1. Enable percentiles on HanLane distribution metrics (p95 only exists from the moment it is on).
2. Upsert the SLO (datadog/slo.json), then the dashboard (datadog/dashboard.json, ${SLO_ID} filled in).
3. Upsert every monitor in datadog/monitors/*.json, matched by name.

    python scripts/dd_apply.py              # apply (needs DD_API_KEY and DD_APP_KEY)
    python scripts/dd_apply.py --validate   # check the JSON against the API models, offline
    python scripts/dd_apply.py --status     # monitor states and SLO status
    python scripts/dd_apply.py --prune      # apply, then delete team:hanlane monitors not defined in the repo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from datadog_api_client import ApiClient, Configuration
from datadog_api_client.exceptions import ApiException, NotFoundException
from datadog_api_client.model_utils import validate_and_convert_types
from datadog_api_client.v1.api.dashboards_api import DashboardsApi
from datadog_api_client.v1.api.monitors_api import MonitorsApi
from datadog_api_client.v1.api.service_level_objectives_api import ServiceLevelObjectivesApi
from datadog_api_client.v1.model.dashboard import Dashboard
from datadog_api_client.v1.model.monitor import Monitor
from datadog_api_client.v1.model.monitor_update_request import MonitorUpdateRequest
from datadog_api_client.v1.model.service_level_objective import ServiceLevelObjective
from datadog_api_client.v1.model.service_level_objective_request import ServiceLevelObjectiveRequest
from datadog_api_client.v2.api.metrics_api import MetricsApi
from datadog_api_client.v2.model.metric_tag_configuration_create_request import MetricTagConfigurationCreateRequest
from datadog_api_client.v2.model.metric_tag_configuration_update_request import MetricTagConfigurationUpdateRequest

from common.settings import ROOT, settings

DD_DIR = ROOT / "datadog"
PERCENTILE_METRICS = [
    "hanlane.loop.latency_ms", "hanlane.detect.lag_ms", "hanlane.ingest.lag_ms", "hanlane.mongo.batch_size",
    "hanlane.segment_ms", "hanlane.mongo.write_ms", "hanlane.changestream.lag_ms",
]
METRIC_TAGS = ["env", "service", "event_type", "segment", "path", "write_mode", "host"]


def configuration() -> Configuration:
    s = settings()
    if not s.dd_api_key or not s.dd_app_key:
        sys.exit("DD_API_KEY and DD_APP_KEY must be set in .env (create an application key in Datadog: "
                 "Organization Settings > Application Keys)")
    cfg = Configuration()
    cfg.api_key["apiKeyAuth"] = s.dd_api_key
    cfg.api_key["appKeyAuth"] = s.dd_app_key
    cfg.server_variables["site"] = s.dd_site
    return cfg


def model(cls, data: dict, cfg: Configuration):
    return validate_and_convert_types(data, (cls,), ["body"], True, True, cfg)


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def monitors_json() -> list[dict]:
    return [load(p) for p in sorted((DD_DIR / "monitors").glob("*.json"))]


# ---------------------------------------------------------------------------- apply
def enable_percentiles(api_client: ApiClient, cfg: Configuration) -> None:
    api = MetricsApi(api_client)
    for metric in PERCENTILE_METRICS:
        attrs = {"tags": METRIC_TAGS, "include_percentiles": True}
        try:
            api.list_tag_configuration_by_name(metric)
            body = {"data": {"type": "manage_tags", "id": metric, "attributes": attrs}}
            api.update_tag_configuration(metric, model(MetricTagConfigurationUpdateRequest, body, cfg))
            print(f"percentiles  updated   {metric}")
        except NotFoundException:
            body = {"data": {"type": "manage_tags", "id": metric,
                             "attributes": {**attrs, "metric_type": "distribution"}}}
            try:
                api.create_tag_configuration(metric, model(MetricTagConfigurationCreateRequest, body, cfg))
                print(f"percentiles  enabled   {metric}")
            except ApiException as e:
                print(f"percentiles  SKIPPED   {metric}: {e.status} {e.body[:160] if e.body else ''}")


def upsert_slo(api_client: ApiClient, cfg: Configuration) -> str:
    api = ServiceLevelObjectivesApi(api_client)
    spec = load(DD_DIR / "slo.json")
    existing = [s for s in api.list_slos(query=spec["name"]).get("data", []) if s["name"] == spec["name"]]
    if existing:
        slo_id = existing[0]["id"]
        api.update_slo(slo_id, model(ServiceLevelObjective, spec, cfg))
        print(f"slo          updated   {spec['name']} ({slo_id})")
    else:
        res = api.create_slo(model(ServiceLevelObjectiveRequest, spec, cfg))
        slo_id = res["data"][0]["id"]
        print(f"slo          created   {spec['name']} ({slo_id})")
    return slo_id


def upsert_dashboard(api_client: ApiClient, cfg: Configuration, slo_id: str) -> str:
    api = DashboardsApi(api_client)
    raw = (DD_DIR / "dashboard.json").read_text().replace("${SLO_ID}", slo_id)
    spec = json.loads(raw)
    existing = [d for d in api.list_dashboards().get("dashboards", []) if d["title"] == spec["title"]]
    body = model(Dashboard, spec, cfg)
    if existing:
        res = api.update_dashboard(existing[0]["id"], body)
        print(f"dashboard    updated   {spec['title']}")
    else:
        res = api.create_dashboard(body)
        print(f"dashboard    created   {spec['title']}")
    return res["url"]


def upsert_monitors(api_client: ApiClient, cfg: Configuration, prune: bool = False) -> None:
    api = MonitorsApi(api_client)
    current = {m["name"]: m for m in api.list_monitors(monitor_tags="team:hanlane")}
    wanted = {spec["name"] for spec in monitors_json()}
    for name, m in current.items():
        if name not in wanted:
            if prune:
                api.delete_monitor(m["id"])
                print(f"monitor      deleted   {name} (not in datadog/monitors)")
            else:
                print(f"monitor      ORPHAN    {name} (not in datadog/monitors; --prune deletes it)")
    for spec in monitors_json():
        if spec["name"] in current:
            upd = {k: v for k, v in spec.items() if k != "type"}
            api.update_monitor(current[spec["name"]]["id"], model(MonitorUpdateRequest, upd, cfg))
            print(f"monitor      updated   {spec['name']}")
        else:
            api.create_monitor(model(Monitor, spec, cfg))
            print(f"monitor      created   {spec['name']}")


def status(api_client: ApiClient) -> None:
    for m in sorted(MonitorsApi(api_client).list_monitors(monitor_tags="team:hanlane"), key=lambda m: m["name"]):
        print(f"{str(m['overall_state']):<10} {m['name']}")
    for s in ServiceLevelObjectivesApi(api_client).list_slos(tags_query="team:hanlane").get("data", []):
        print(f"{'SLO':<10} {s['name']} ({s['id']})")


def validate() -> int:
    cfg = Configuration()
    model(ServiceLevelObjectiveRequest, load(DD_DIR / "slo.json"), cfg)
    model(Dashboard, json.loads((DD_DIR / "dashboard.json").read_text().replace("${SLO_ID}", "x")), cfg)
    for spec in monitors_json():
        model(Monitor, spec, cfg)
    print(f"valid: slo.json, dashboard.json, {len(monitors_json())} monitors")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--prune", action="store_true", help="delete team:hanlane monitors not defined in the repo")
    args = ap.parse_args()
    if args.validate:
        return validate()
    cfg = configuration()
    with ApiClient(cfg) as api_client:
        if args.status:
            status(api_client)
            return 0
        enable_percentiles(api_client, cfg)
        slo_id = upsert_slo(api_client, cfg)
        url = upsert_dashboard(api_client, cfg, slo_id)
        upsert_monitors(api_client, cfg, prune=args.prune)
        site = settings().dd_site
        host = "app.datadoghq.com" if site == "datadoghq.com" else site
        print(f"\ndashboard: https://{host}{url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
