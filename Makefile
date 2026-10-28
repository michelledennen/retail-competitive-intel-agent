.PHONY: install test lint format web run deploy

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

test:
	.venv/bin/pytest -v

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

# Chat with the agent in your browser (http://localhost:8000)
web:
	.venv/bin/adk web .

# Chat with the agent in the terminal
run:
	.venv/bin/adk run retail_intel_agent

# Deploy to Cloud Run with Cloud Trace enabled (requires gcloud + a GCP project)
deploy:
	./deployment/deploy_cloud_run.sh
