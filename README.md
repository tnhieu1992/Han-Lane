# HanLane

Real-time data layer for autonomous vehicles in central Da Nang. Simulated vehicles (SUMO) stream telemetry into MongoDB Atlas; the gateway detects hazards ahead (a stopped vehicle, hard braking, a jammed lane) and picks the vehicles behind; the worker relays the warning back to them. Datadog measures the whole loop, from the record that triggered the rule (t0) to the moment the vehicle applies the command (t5). Target: p95 under 1 second, the V2N budget for hazard warnings 50–150 m ahead (3GPP TS 22.185).

It does not drive the cars. What it shows is the data path, the loop latency, observability, and an A/B-measured effect.

## Architecture

```
sim-bridge (SUMO + TraCI, 0.2 s steps) --1 batch per second--> ingest-gateway (FastAPI)
  gateway: rules on in-RAM state -> recipients -> events.insert_one (with trace context)
  gateway: telemetry.insert_many(ordered=False), not awaited
events --change stream (inserts)--> advisory-worker --HTTP--> sim-bridge queue -> applied next step
gateway --fast path HTTP--> worker (same event; first arrival wins, change stream is the fallback)
gateway --WebSocket--> web map (MapLibre + TrackAsia), never queries Atlas
every Python service --> Datadog Agent (APM, DogStatsD, logs)
```

| Service | Path | Port |
| --- | --- | --- |
| sim-bridge | `sim/bridge.py` | 8100 (control) |
| ingest-gateway | `gateway/app.py` | 8000 |
| advisory-worker | `worker/main.py` | 8200 (fast path) |
| web map | `web/` | 5180 |
| Datadog Agent | `docker-compose.yml` | 8126, 8125/udp |

## Measured (this repo, Atlas shared tier, ~60 vehicles)

| What | Value |
| --- | --- |
| Loop t0→t5 p95 / p50 (probes, fast path) | ~201 ms / ~197 ms (floor set by the 0.2 s SUMO step) |
| t3→t4 change stream p95 | ~540 ms (over the 400 ms budget, hence the fast path) |
| t3→t4 fast path p95 | ~6 ms |
| Detection t0→t3 | < 10 ms |
| `insert_many` of 50 docs | p50 145 ms, p95 227 ms |
| Write ceiling | ~100 documents/s regardless of batching (`docs/measure_m0*.json`) |
| Telemetry on disk | ~249 bytes per record including indexes |

Details and the reasoning behind each choice: `docs/decisions.md`. A/B results: `docs/ab_results.md`.

## Run it

Requirements: Python 3.12, Docker, Node 20+, a MongoDB Atlas cluster, a Datadog account.

```bash
cp .env.example .env     # MONGODB_URI, DD_API_KEY, DD_APP_KEY, DD_SITE, MAP_STYLE_URL
make venv                # .venv with every Python dependency, SUMO included (eclipse-sumo wheel)
make init-db             # collections + indexes, safe to re-run
make segments            # road_segments from the SUMO net (once)
(cd web && npm ci)       # web dependencies (once)
make demo                # agent + gateway + worker + sim + web; open http://localhost:5180
```

`make demo` warms the simulation up for 150 simulated seconds so the Dragon Bridge has traffic. `GUI=1 make demo` opens sumo-gui as well.

In the browser: **Tạo sự cố · Cầu Rồng** stops a vehicle on the bridge (hard brake, then hazard lights). Vehicles behind it turn orange as advisories land; the panel shows loop latency. **Dọn sự cố** lets it drive on. The other buttons toggle advisories, `WRITE_MODE` (`batch` / `single`) and x3 traffic.

Same actions from the terminal: `make incident`, `make clear`.

### Datadog

```bash
make dd-apply            # percentiles, SLO, dashboard, monitors from datadog/*.json (needs DD_APP_KEY)
make dd-status           # monitor states
```

Percentiles on distribution metrics only exist from the moment they are enabled, so run `make dd-apply` early. Never edit the dashboard or monitors in the UI; change the JSON and re-apply.

### Experiments

```bash
make ab                  # same incident twice, advisories off then on (stop the demo sim first)
make rush                # x3 vehicles, then WRITE_MODE=single for 3 minutes
make measure             # M0 storage, bytes and write ceiling
make rollup              # per-edge minute rollup with $merge (the gateway also runs it every minute)
```

## Rebuilding the scenario

`make net` downloads OpenStreetMap data for the bbox in `config/hanlane.toml`, runs `netconvert` (junction joining, guessed traffic lights), writes vehicle types (85% motorbikes, 12% cars, 3% buses), random trips plus a dense flow across the Dragon Bridge, and the edge-to-zone map. The generated net and routes are committed, so this is only needed to change the area.

## Layout

```
common/    settings, Mongo client (zstd), JSON logging, DogStatsD, road graph helpers
config/    hanlane.toml: rule thresholds, TTLs, sim parameters, latency budget
db/        init.py (idempotent schema), load_segments.py, rollup.py ($merge)
sim/       build_net.py, bridge.py (TraCI loop), control_server.py, net/ (generated scenario)
gateway/   app.py, state.py (RAM), rules.py, recipients.py, writer.py
worker/    main.py (change stream + fast path)
web/       Vite + MapLibre GL
scripts/   incident, measure_m0, ab_run, rush_hour, dd_apply, demo.sh
datadog/   dashboard.json, monitors/*.json, slo.json, agent log config
docs/      decisions, known issues, demo script, measurements
```

## Assumptions

- Lane-level position comes from SUMO. A real deployment needs lane-level positioning or on-board cameras.
- Vehicle ids are pseudonyms that change per trip; raw telemetry expires after 2 hours (TTL), only rollups stay longer.
- Out of scope: driving AI, image recognition, mobile app, user login, collision avoidance under 2 s headway.

See `docs/known-issues.md` for what is known not to work or not to scale.
