"""Persistent session + memory services.

* **Sessions** -- :class:`google.adk.sessions.DatabaseSessionService` backed
  by SQLite (``sqlite+aiosqlite:///.adk/sessions.db`` by default). Session
  events, session state and ``user:``-scoped state (focus areas, saved
  insights) survive restarts. Swap the URL for Cloud SQL / AlloyDB
  (``postgresql+asyncpg://...``) in production -- no code changes.
* **Memory** -- ADK's SQLite memory service (``SqliteMemoryService``) so
  ``load_memory`` can search past conversations after a restart.

``adk web`` uses the same backends via
``--session_service_uri sqlite+aiosqlite:///.adk/sessions.db
--memory_service_uri sqlite:///.adk/memory.db`` (see the Makefile).
"""

from __future__ import annotations

from pathlib import Path

from google.adk.memory import BaseMemoryService
from google.adk.sessions import BaseSessionService

from . import config


def _ensure_sqlite_dir(url_or_path: str) -> None:
    """Creates the parent folder of a SQLite file so first runs don't fail."""
    path = url_or_path.split(":///", 1)[-1] if ":///" in url_or_path else url_or_path
    if path and path != ":memory:" and "://" not in path:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def create_session_service(db_url: str | None = None) -> BaseSessionService:
    """Returns a DatabaseSessionService for ``db_url`` (default: config.SESSION_DB_URL)."""
    from google.adk.sessions import DatabaseSessionService  # needs sqlalchemy[asyncio]

    url = db_url or config.SESSION_DB_URL
    if url.startswith("sqlite"):
        _ensure_sqlite_dir(url)
    return DatabaseSessionService(db_url=url)


def create_memory_service(db_path: str | None = None) -> BaseMemoryService:
    """Returns a persistent SQLite memory service (default: config.MEMORY_DB_PATH)."""
    from google.adk.memory import SqliteMemoryService

    path = db_path or config.MEMORY_DB_PATH
    _ensure_sqlite_dir(path)
    return SqliteMemoryService(db_path=path)
