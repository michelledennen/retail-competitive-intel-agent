"""Central, environment-driven configuration.

Everything here can be overridden with environment variables (or the
``retail_intel_agent/.env`` file that ``adk web`` loads automatically), so the
same code runs locally, in CI and on Cloud Run without edits.

Secrets are NEVER hardcoded. ``GOOGLE_API_KEY`` comes from the environment, or
-- in production -- from Google Secret Manager when ``GOOGLE_API_KEY_SECRET`` is
set (see :func:`load_google_api_key`).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("retail_intel_agent.config")

APP_NAME = "retail_intel_agent"

# --------------------------------------------------------------------------
# Strategic model routing
# --------------------------------------------------------------------------
# PRO_MODEL  -> reasoning-heavy agents (coordinator, strategy writer, reviewer).
# FAST_MODEL -> cheap, low-latency specialists that mostly call tools
#               (financial data lookups, web search).
# With a paid key you can set PRO_MODEL=gemini-3.1-pro-preview for deeper
# reasoning on the coordinator / writer / reviewer.
PRO_MODEL = os.getenv("PRO_MODEL", "gemini-3.5-flash")
FAST_MODEL = os.getenv("FAST_MODEL", "gemini-3.5-flash-lite")

# --------------------------------------------------------------------------
# Context management
# --------------------------------------------------------------------------
# Summarize older turns every N user messages so long strategy sessions stay
# within the model's context window.
COMPACTION_INTERVAL = int(os.getenv("COMPACTION_INTERVAL", "5"))
COMPACTION_OVERLAP = int(os.getenv("COMPACTION_OVERLAP", "1"))

# --------------------------------------------------------------------------
# Persistence (used by run_cli.py, `make web` and the Dockerfile)
# --------------------------------------------------------------------------
# DatabaseSessionService URL (SQLAlchemy async URL). SQLite locally; point it
# at Cloud SQL / AlloyDB (postgresql+asyncpg://...) in production.
SESSION_DB_URL = os.getenv("SESSION_DB_URL", "sqlite+aiosqlite:///.adk/sessions.db")
MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH", ".adk/memory.db")

# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------
ENABLE_CLOUD_TRACE = os.getenv("ENABLE_CLOUD_TRACE", "false").lower() in ("1", "true", "yes")


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def load_google_api_key() -> str | None:
    """Returns the Gemini API key without ever hardcoding it.

    Resolution order:
      1. ``GOOGLE_API_KEY`` environment variable (local dev, CI secret).
      2. Google Secret Manager, when ``GOOGLE_API_KEY_SECRET`` holds a secret
         resource name such as
         ``projects/my-proj/secrets/google-api-key/versions/latest``.
         The client library is imported lazily so it is only required in
         environments that actually use Secret Manager.

    When the key is fetched from Secret Manager it is exported to
    ``GOOGLE_API_KEY`` for the google-genai client. The key value is never
    logged.

    Returns:
        The API key, or ``None`` if neither source is configured (for example
        when using Vertex AI with Application Default Credentials).
    """
    key = os.getenv("GOOGLE_API_KEY")
    if key:
        return key
    secret_name = os.getenv("GOOGLE_API_KEY_SECRET")
    if not secret_name:
        return None
    try:
        from google.cloud import secretmanager  # lazy: optional dependency
    except ImportError:
        logger.warning(
            "GOOGLE_API_KEY_SECRET is set but google-cloud-secret-manager is not "
            "installed. Run `pip install google-cloud-secret-manager`."
        )
        return None
    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=secret_name)
        key = response.payload.data.decode("utf-8").strip()
    except Exception as exc:  # never crash agent import on secret errors
        logger.error("Could not read secret %s: %s", secret_name, type(exc).__name__)
        return None
    os.environ["GOOGLE_API_KEY"] = key
    return key


# Resolve the key at import time (no-op when GOOGLE_API_KEY is already set or
# when running on Vertex AI with ADC).
if not _env_true("GOOGLE_GENAI_USE_VERTEXAI"):
    load_google_api_key()
