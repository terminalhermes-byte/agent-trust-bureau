.PHONY: setup migrate test run dev devup bootstrap create-key worker worker-once

VENV := .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python
ALEMBIC := $(VENV)/bin/alembic
PYTEST := $(VENV)/bin/pytest
UVICORN := $(VENV)/bin/uvicorn

setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e .
	@test -f .env || cp .env.example .env
	@echo "Done. Activate with: source $(VENV)/bin/activate"

migrate:
	$(ALEMBIC) upgrade head

test:
	$(PYTHON) -m pytest -v

run:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8010

dev:
	$(UVICORN) app.main:app --reload --port 8010

devup: migrate dev

bootstrap: migrate
	$(PYTHON) -m app.cli bootstrap

create-key:
	$(PYTHON) -m app.cli create-key --tenant $(TENANT) --name $(or $(NAME),default)

worker:
	$(PYTHON) -m app.worker

worker-once:
	$(PYTHON) -m app.worker --once
