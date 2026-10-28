"""Tests for persistent sessions/memory and secret loading (offline)."""

from __future__ import annotations

import pytest

from retail_intel_agent import config
from retail_intel_agent.tools.retail_tools import STATE_FOCUS_AREAS

sqlalchemy = pytest.importorskip("sqlalchemy", reason="pip install 'sqlalchemy[asyncio]'")


@pytest.mark.asyncio
async def test_database_session_service_persists_user_state_across_restarts(tmp_path):
    from google.adk.events import Event, EventActions
    from google.adk.sessions import DatabaseSessionService

    from retail_intel_agent.persistence import create_session_service

    url = f"sqlite+aiosqlite:///{tmp_path / 'nested' / 'sessions.db'}"
    service = create_session_service(url)
    assert isinstance(service, DatabaseSessionService)
    session = await service.create_session(app_name=config.APP_NAME, user_id="u1")
    await service.append_event(
        session,
        Event(
            author="user",
            invocation_id="inv-1",
            actions=EventActions(state_delta={STATE_FOCUS_AREAS: ["grocery"], "temp:x": 1}),
        ),
    )
    await service.close()

    # "Restart": a brand-new service instance on the same SQLite file.
    restarted = create_session_service(url)
    again = await restarted.get_session(app_name=config.APP_NAME, user_id="u1", session_id=session.id)
    assert again is not None
    assert again.state[STATE_FOCUS_AREAS] == ["grocery"]
    assert "temp:x" not in again.state  # temp: state is never persisted
    # user: state is shared with the same user's NEW sessions.
    other = await restarted.create_session(app_name=config.APP_NAME, user_id="u1")
    assert other.state[STATE_FOCUS_AREAS] == ["grocery"]
    await restarted.close()


def test_memory_service_is_persistent_sqlite(tmp_path):
    from google.adk.memory import SqliteMemoryService

    from retail_intel_agent.persistence import create_memory_service

    service = create_memory_service(str(tmp_path / "mem" / "memory.db"))
    assert isinstance(service, SqliteMemoryService)
    assert (tmp_path / "mem").is_dir()


def test_runner_wires_persistent_services(tmp_path):
    from google.adk.runners import Runner

    from retail_intel_agent.agent import app
    from retail_intel_agent.persistence import create_memory_service, create_session_service

    runner = Runner(
        app=app,
        session_service=create_session_service(f"sqlite+aiosqlite:///{tmp_path / 's.db'}"),
        memory_service=create_memory_service(str(tmp_path / "m.db")),
    )
    assert runner.app_name == config.APP_NAME
