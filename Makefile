# Downtime Copilot. See README "Running it".
# Credentials: `source .env` first (export lines). Mock mode: `DRY_RUN=1 make dev`.

VENV    := .venv
PY      := $(VENV)/bin/python
DEPS    := boto3 fastapi uvicorn pydantic pytest httpx python-multipart pillow
PROBE   := tools/bedrock_probe.py
SIM_APP ?= simulator.main:app
API_APP ?= app.main:app
APP_PORT ?= 8000
SIM_PORT ?= 8001
SIM_URL ?= http://localhost:$(SIM_PORT)
APP_URL ?= http://localhost:$(APP_PORT)/?phase=before
export APP_PORT SIM_PORT SIM_URL

.PHONY: setup probe schemas forms dev demo test evals results guard export-audit e2e

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

forms:  ## regenerate printable form templates, layout maps and seeded fixtures (data/forms/)
	$(PY) tools/form_fixtures.py

dev:  ## simulator on :$(SIM_PORT) (default 8001), app on :$(APP_PORT) (default 8000); Ctrl-C stops both
	@$(PY) -m uvicorn $(SIM_APP) --port $(SIM_PORT) --reload & sim=$$!; \
	$(PY) -m uvicorn $(API_APP) --port $(APP_PORT) --reload & api=$$!; \
	trap 'kill $$sim $$api 2>/dev/null' INT TERM EXIT; \
	wait

demo:  ## clean store + 12 patients at "Before" with a healthy EHR; prints the URL (DEMO_OPEN=1 also opens it). Needs `make dev`
	$(PY) -m simulator.seed --fresh
	@echo "Demo ready: $(APP_URL)"
	@if [ "$(DEMO_OPEN)" = "1" ]; then \
		if command -v open >/dev/null; then open "$(APP_URL)"; else xdg-open "$(APP_URL)" || true; fi; \
	fi

export-audit:  ## audit log -> exports/ in AUDIO_BUCKET (local file in DRY_RUN). AT=2026-10-24T09:00:00Z re-names one export (idempotent). Needs `make dev`
	@curl -fsS -X POST "http://localhost:$(APP_PORT)/sync/export$(if $(AT),?at=$(AT))"; echo

test:  ## unit tests
	$(PY) -m pytest -q

e2e:  ## the demo script end to end through the API, mock mode (tests/test_e2e.py)
	DRY_RUN=1 $(PY) -m pytest -q tests/test_e2e.py

evals:  ## ONE live run of evals/manifest.json -> evals/runs/<run-id>/ + RESULTS.md. Needs `source .env`, DRY_RUN=0, a fresh scratch STORE_PATH and the simulator on SIM_PORT (refuses otherwise)
	$(PY) -m evals.run

results:  ## regenerate RESULTS.md from the newest committed bundle (evals/baseline/<run-id>/ + evals/NOTES.md); pure, no network. BUNDLE=<dir> overrides
	$(PY) -m evals.report $(BUNDLE)

guard:  ## secret / probe guard (run before every commit)
	bash tools/guard.sh
