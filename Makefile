.PHONY: install install-eval test lint format web run cli eval evalset deploy tf-init tf-plan tf-apply

# Persistent local storage for `adk web` (DatabaseSessionService + SQLite memory).
SESSION_URI ?= sqlite+aiosqlite:///.adk/sessions.db
MEMORY_URI  ?= sqlite:///.adk/memory.db

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

install-eval:
	.venv/bin/pip install -r requirements-eval.txt

test:
	.venv/bin/pytest -v

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

# Chat with the agent in your browser (http://localhost:8000).
# Sessions, user state and memory persist in .adk/ across restarts.
web:
	mkdir -p .adk
	.venv/bin/adk web --session_service_uri $(SESSION_URI) --memory_service_uri $(MEMORY_URI) .

# Chat with the agent in the terminal (ADK's built-in CLI, in-memory).
run:
	.venv/bin/adk run retail_intel_agent

# Terminal chat with persistent sessions + memory + approve/reject prompts.
cli:
	.venv/bin/python run_cli.py

# Run the golden evaluation set against the live agent (needs GOOGLE_API_KEY
# and `make install-eval`).
eval:
	.venv/bin/adk eval retail_intel_agent eval/data/retail_intel.evalset.json \
		--config_file_path eval/data/test_config.json --print_detailed_results

# Regenerate eval/data/*.json from eval/build_evalset.py.
evalset:
	.venv/bin/python eval/build_evalset.py

# Deploy to Cloud Run with Cloud Trace enabled (requires gcloud + a GCP project)
deploy:
	./deployment/deploy_cloud_run.sh

# Infrastructure as Code (see terraform/README.md)
tf-init:
	terraform -chdir=terraform init

tf-plan:
	terraform -chdir=terraform plan

tf-apply:
	terraform -chdir=terraform apply
