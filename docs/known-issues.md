# Known issues and limits

| # | Issue | Impact | Workaround / next step |
| --- | --- | --- | --- |
| 1 | Atlas free/shared tier throttles writes at ~100 documents/s, whether sent with `insert_one` or `insert_many` (`docs/measure_m0*.json`). | Fleet limited to ~60–90 vehicles at 1 Hz; the 300-vehicle target in the design needs a paid tier. x3 load (~180 vehicles) builds a write backlog by design. | Demo at ~60 vehicles (`sim.scale = 0.3`). The load test shows the ceiling on purpose. |
| 2 | Loop latency has a ~200 ms floor. | Commands are applied at the next 0.2 s SUMO step, and t0 is read on a step boundary, so t5 − t0 ≈ one step. | By design. A 0.1 s step would halve it at a CPU cost. |
| 3 | Change stream delivery on the shared tier: p95 ~540 ms (t3→t4). | Over the 400 ms hop budget. | Fast path enabled (`[loop] fast_path`); change stream kept as fallback and measured as `hanlane.changestream.lag_ms`. |
| 4 | Percentiles were enabled on 2026-11-16 ~00:15. | p95/p99 of HanLane distribution metrics do not exist before that (not backfilled); avg/max do. | None needed now; keep `make dd-apply` in the setup steps for new environments. |
| 5 | With `--fast-until N` (warm-up), the simulated clock `ts` runs up to N seconds ahead of wall time. | `ts` is anchored to the wall clock at start; rollup minutes are shifted by the warm-up. Latency uses `t0`/`t5` (wall clock) and is not affected. | Accepted. Use `WARMUP=0` if aligned timestamps matter. |
| 6 | LANE_JAM uses telemetry only; `rsu_readings` exists but is empty. | No roadside detector data. | RSU was cut first per the cut order. |
| 7 | Hanoi traffic mode and replay were not built. | | Cut per the cut order. |
| 8 | A/B is one pair of runs with one seed (harsh braking 15 → 6, emergency 9 → 5). | Direction of the effect, not statistical significance. | Run `make ab` with several seeds (`--seed`) and report the spread. |
| 9 | STALLED without hazard lights can still fire on long queues at unsignalised junctions. | Occasional false positives. | Vehicles within 15 m of the end of their lane are ignored (`stalled_junction_clear_m`). |
| 10 | The TrackAsia key is visible to the browser (it is in the style URL). | Anyone using the page can read it. | Restrict the key by HTTP referrer in the TrackAsia console. |
| 11 | `make ab` needs the demo sim stopped (both use port 8100). | | Stop `make demo` first. |
| 12 | Datadog site in the design doc is `ap1`; the account in `.env` is US1 (`datadoghq.com`). | | Everything reads `DD_SITE`; nothing is hard-coded. |
| 13 | Port 5173 is used by another local app. | | The web runs on 5180 with `--strictPort`. |
| 14 | After a x3 load test, setting demand back to x1 takes several minutes to take effect (vehicles already loaded keep departing) and the telemetry write backlog stays full until the fleet is under ~90 vehicles. | The map and Datadog keep showing load for minutes. | Restart the sim (`make demo` again, or restart `make sim`) after the load test. |
| 15 | Single writes on the shared tier are slow even at normal load (gateway PyMongo p95 0.25–1.1 s). | A 300 ms PyMongo monitor would alert permanently. | Monitor on ingest lag instead; PyMongo p95 monitor at 1.5 s (see decisions). |
