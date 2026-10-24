# Downtime Copilot. See README "Running it".
# Credentials: `source .env` first (export lines). Mock mode: `DRY_RUN=1 make dev`.

VENV    := .venv
PY      := $(VENV)/bin/python
DEPS    := boto3 fastapi uvicorn pydantic pytest httpx python-multipart
PROBE   := tools/bedrock_probe.py
SIM_APP ?= simulator.main:app
API_APP ?= app.main:app
APP_URL ?= http://localhost:8000/?phase=before

.PHONY: setup probe schemas dev demo test evals guard

setup:  ## venv + dependencies
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q $(DEPS)

probe:  ## what this AWS account allows; decides fallbacks (docs/decisions.md)
	@if [ ! -f $(PROBE) ]; then \
		echo "$(PROBE) not found: probe is distributed by DM, not in git. Save it to $(PROBE) (it is git-ignored)."; \
		exit 1; \
	fi
	$(PY) $(PROBE) --extras

schemas:  ## re-export contracts/schema/*.json after a contract change
	$(PY) -m contracts.export_schemas

dev:  ## simulator on :8001, app on :8000 (Ctrl-C stops both)
	@if [ ! -d simulator ] || [ ! -d app ]; then \
		echo "TODO: simulator/ and app/ arrive in a later slice; nothing to run yet."; \
		exit 1; \
	fi
	@$(PY) -m uvicorn $(SIM_APP) --port 8001 --reload & sim=$$!; \
	$(PY) -m uvicorn $(API_APP) --port 8000 --reload & api=$$!; \
	trap 'kill $$sim $$api 2>/dev/null' INT TERM EXIT; \
	wait

demo:  ## seeds 12 patients, opens the app at "Before" with a healthy EHR
	@# TODO: replace with the real seed command once simulator/ lands (Role 2).
	@if [ ! -d simulator ]; then \
		echo "TODO: demo seeding arrives with simulator/ in a later slice."; \
		exit 1; \
	fi
	$(PY) -m simulator.seed
	@command -v open >/dev/null && open "$(APP_URL)" || xdg-open "$(APP_URL)" || echo "open $(APP_URL)"

test:  ## unit tests
	$(PY) -m pytest -q

evals:  ## generates RESULTS.md (never hand-edit it)
	@# TODO: evals harness arrives in a later slice (Role 5 P1).
	@if [ ! -d evals ]; then \
		echo "TODO: evals/ harness arrives in a later slice; RESULTS.md not generated."; \
		exit 1; \
	fi
	$(PY) -m evals.run

guard:  ## secret / probe guard (run before every commit)
	bash tools/guard.sh
