"""Programmatic runner with persistent sessions, memory and human-in-the-loop.

Usage:
    python run_cli.py                       # new session for user "demo"
    python run_cli.py --user alice --session my-session   # resume a session

Unlike ``adk run``, this runner wires the agent to:
  * DatabaseSessionService (SQLite) -- state survives restarts,
  * SqliteMemoryService -- ``load_memory`` can recall past conversations,
  * an interactive approve/reject prompt for tools that require confirmation
    (``save_insight``, ``clear_user_memory``).
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from google.adk.runners import Runner
from google.genai import types

from retail_intel_agent.agent import app
from retail_intel_agent.persistence import create_memory_service, create_session_service

CONFIRMATION_CALL = "adk_request_confirmation"


def _confirmation_reply(call: types.FunctionCall, approved: bool) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id, name=CONFIRMATION_CALL, response={"confirmed": approved}
                )
            )
        ],
    )


async def _send(runner: Runner, user_id: str, session_id: str, message: types.Content) -> None:
    pending: list[types.FunctionCall] = []
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
        for call in event.get_function_calls() or []:
            if call.name == CONFIRMATION_CALL:
                pending.append(call)
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts)
            if text.strip():
                print(f"\nagent> {text}\n")
    for call in pending:
        original = (call.args or {}).get("originalFunctionCall", {})
        tool_name = original.get("name", "a tool")
        answer = input(f"[approval needed] Allow {tool_name}({original.get('args', {})})? [y/N] ")
        await _send(runner, user_id, session_id, _confirmation_reply(call, answer.strip().lower() == "y"))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", default="demo")
    parser.add_argument("--session", default=None, help="Session id to resume.")
    args = parser.parse_args()

    runner = Runner(
        app=app,
        session_service=create_session_service(),
        memory_service=create_memory_service(),
    )
    session_id = args.session or f"cli-{uuid.uuid4().hex[:8]}"
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=args.user, session_id=session_id
    )
    if session is None:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=args.user, session_id=session_id
        )
    print(f"Session '{session.id}' for user '{args.user}' (persisted). Type 'exit' to quit.")
    try:
        while True:
            text = input("you> ").strip()
            if text.lower() in {"exit", "quit"}:
                break
            if text:
                await _send(runner, args.user, session.id, types.Content(role="user", parts=[types.Part(text=text)]))
    finally:
        await runner.close()


if __name__ == "__main__":
    asyncio.run(main())
