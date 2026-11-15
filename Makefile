PY      := .venv/bin/python
RUN     := $(PY) -m common.envrun
DDRUN   := .venv/bin/ddtrace-run
SIM_ARGS ?=

.PHONY: venv init-db check-db net segments agent-up agent-status down gateway worker sim sim-gui web demo stop measure incident clear ab rush dd-apply dd-status rollup

venv:            ## create .venv (Python 3.12) and install deps
	python3.12 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -r requirements.txt -r requirements-sim.txt

init-db:         ## create collections and indexes (idempotent)
	$(RUN) hanlane-init $(PY) db/init.py

check-db:
	$(RUN) hanlane-init $(PY) db/init.py --check

net:             ## download OSM, build SUMO net, routes, zones
	$(RUN) hanlane-sim $(PY) sim/build_net.py

segments:        ## load road_segments from the SUMO net
	$(RUN) hanlane-init $(PY) db/load_segments.py

agent-up:        ## start the Datadog Agent
	docker compose up -d datadog-agent

agent-status:
	docker compose exec datadog-agent agent status

down:
	docker compose down

gateway:         ## run ingest-gateway on the host (port 8000)
	$(RUN) hanlane-gateway $(DDRUN) .venv/bin/uvicorn gateway.app:app --host 0.0.0.0 --port 8000

worker:          ## run advisory-worker on the host
	$(RUN) hanlane-worker $(DDRUN) $(PY) -m worker.main

sim:             ## run sim-bridge headless (SIM_ARGS="--scale 2")
	$(RUN) hanlane-sim $(DDRUN) $(PY) -m sim.bridge $(SIM_ARGS)

sim-gui:
	$(RUN) hanlane-sim $(DDRUN) $(PY) -m sim.bridge --gui $(SIM_ARGS)

web:             ## run the web map (Vite dev server, port 5180)
	cd web && npm run dev

demo:            ## agent + gateway + worker + sim + web in one go (Ctrl-C stops all)
	./scripts/demo.sh

measure:
	$(RUN) hanlane-measure $(PY) scripts/measure_m0.py

incident:
	$(RUN) hanlane-cli $(PY) scripts/incident.py

clear:
	$(RUN) hanlane-cli $(PY) scripts/incident.py --clear

ab:
	$(RUN) hanlane-ab $(PY) scripts/ab_run.py

rush:
	$(RUN) hanlane-cli $(PY) scripts/rush_hour.py

dd-apply:
	$(RUN) hanlane-cli $(PY) scripts/dd_apply.py

dd-status:
	$(RUN) hanlane-cli $(PY) scripts/dd_apply.py --status

rollup:
	$(RUN) hanlane-cli $(PY) db/rollup.py
