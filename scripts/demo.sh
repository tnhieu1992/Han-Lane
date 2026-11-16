#!/usr/bin/env bash
# Start the whole demo: Datadog Agent, gateway, worker, sim-bridge, web map. Ctrl-C stops everything.
#   ./scripts/demo.sh            # headless SUMO, 150 s warm-up so the bridge has traffic
#   GUI=1 ./scripts/demo.sh      # with sumo-gui
#   WARMUP=0 ./scripts/demo.sh   # no warm-up
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
WARMUP="${WARMUP:-150}"
pids=()

cleanup() {
  echo; echo "stopping..."
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  echo "stopped (Datadog Agent keeps running: make down)"
}
trap cleanup INT TERM EXIT

wait_http() {
  for _ in $(seq 1 60); do curl -sf -o /dev/null "$1" && return 0; sleep 1; done
  echo "timeout waiting for $1"; exit 1
}

docker compose up -d datadog-agent >/dev/null
echo "datadog-agent  up"

make -s gateway > logs/run-gateway.out 2>&1 & pids+=($!)
make -s worker  > logs/run-worker.out 2>&1 & pids+=($!)
wait_http http://localhost:8000/healthz; echo "gateway        http://localhost:8000"
wait_http http://localhost:8200/;        echo "worker         fast path :8200"

SIM_ARGS="--fast-until ${WARMUP}"
[[ "${GUI:-0}" == "1" ]] && target=sim-gui || target=sim
make -s "$target" SIM_ARGS="$SIM_ARGS" > logs/run-sim.out 2>&1 & pids+=($!)
wait_http http://localhost:8100/status;  echo "sim-bridge     control :8100 (warm-up ${WARMUP}s sim time)"

(cd web && npm run dev > ../logs/run-web.out 2>&1) & pids+=($!)
wait_http http://localhost:5180/;        echo "web            http://localhost:5180"

echo "logs in logs/run-*.out · Ctrl-C to stop"
wait
