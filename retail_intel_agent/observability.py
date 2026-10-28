"""Observability for the Retail Competitive Intelligence Agent.

Three layers:

1. **Structured logs** -- every agent, model and tool step is logged as one
   JSON line (easy to search locally or in Cloud Logging).
2. **Per-run metrics** -- model calls, tool calls, token usage, errors and
   latency are aggregated per invocation and logged when the run finishes.
3. **Distributed tracing** -- ADK emits OpenTelemetry spans automatically.
   They appear in the "Trace" tab of ``adk web`` and can be exported to
   Google Cloud Trace with ``adk web --trace_to_cloud`` or
   ``adk deploy ... --trace_to_cloud``.

The plugin also implements graceful tool error handling: if a tool raises,
the agent receives a structured error instead of crashing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

LOGGER_NAME = "retail_intel_agent"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, default=str)


def configure_logging() -> logging.Logger:
    """Configures a JSON logger (level controlled by the LOG_LEVEL env var)."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    return logger


def _log(logger: logging.Logger, message: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields})


def _truncate(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


class ObservabilityPlugin(BasePlugin):
    """Logs every step and aggregates per-invocation metrics."""

    def __init__(self, name: str = "observability_plugin") -> None:
        super().__init__(name=name)
        self.logger = configure_logging()
        self._run_start: dict[str, float] = {}
        self._tool_start: dict[str, float] = {}
        self._model_start: dict[str, float] = {}
        self.metrics: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "model_calls": 0,
                "tool_calls": 0,
                "tool_errors": 0,
                "model_errors": 0,
                "prompt_tokens": 0,
                "output_tokens": 0,
                "agents_invoked": [],
                "tools_used": [],
            }
        )

    # ---- run lifecycle -------------------------------------------------
    async def before_run_callback(self, *, invocation_context: InvocationContext) -> None:
        inv = invocation_context.invocation_id
        self._run_start[inv] = time.perf_counter()
        _log(
            self.logger,
            "run_started",
            invocation_id=inv,
            session_id=invocation_context.session.id,
            user_id=invocation_context.user_id,
        )
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        inv = invocation_context.invocation_id
        started = self._run_start.pop(inv, time.perf_counter())
        summary = dict(self.metrics.pop(inv, {}))
        summary["latency_ms"] = round((time.perf_counter() - started) * 1000)
        _log(self.logger, "run_completed", invocation_id=inv, **summary)

    # ---- agents --------------------------------------------------------
    async def before_agent_callback(self, *, agent, callback_context: CallbackContext):
        m = self.metrics[callback_context.invocation_id]
        m["agents_invoked"].append(agent.name)
        _log(
            self.logger,
            "agent_started",
            invocation_id=callback_context.invocation_id,
            agent=agent.name,
        )
        return None

    async def after_agent_callback(self, *, agent, callback_context: CallbackContext):
        _log(
            self.logger,
            "agent_completed",
            invocation_id=callback_context.invocation_id,
            agent=agent.name,
        )
        return None

    # ---- models --------------------------------------------------------
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        key = f"{callback_context.invocation_id}:{callback_context.agent_name}"
        self._model_start[key] = time.perf_counter()
        self.metrics[callback_context.invocation_id]["model_calls"] += 1
        _log(
            self.logger,
            "model_request",
            level=logging.DEBUG,
            invocation_id=callback_context.invocation_id,
            agent=callback_context.agent_name,
            model=llm_request.model,
            num_contents=len(llm_request.contents or []),
        )
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        key = f"{callback_context.invocation_id}:{callback_context.agent_name}"
        started = self._model_start.pop(key, time.perf_counter())
        m = self.metrics[callback_context.invocation_id]
        usage = llm_response.usage_metadata
        prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (usage.candidates_token_count or 0) if usage else 0
        m["prompt_tokens"] += prompt_tokens
        m["output_tokens"] += output_tokens
        _log(
            self.logger,
            "model_response",
            invocation_id=callback_context.invocation_id,
            agent=callback_context.agent_name,
            latency_ms=round((time.perf_counter() - started) * 1000),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            error_code=llm_response.error_code,
        )
        return None

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
    ) -> Optional[LlmResponse]:
        self.metrics[callback_context.invocation_id]["model_errors"] += 1
        _log(
            self.logger,
            "model_error",
            level=logging.ERROR,
            invocation_id=callback_context.invocation_id,
            agent=callback_context.agent_name,
            error=repr(error),
        )
        return None  # let ADK surface the error

    # ---- tools ---------------------------------------------------------
    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict]:
        key = f"{tool_context.invocation_id}:{tool_context.function_call_id}"
        self._tool_start[key] = time.perf_counter()
        m = self.metrics[tool_context.invocation_id]
        m["tool_calls"] += 1
        m["tools_used"].append(tool.name)
        _log(
            self.logger,
            "tool_called",
            invocation_id=tool_context.invocation_id,
            agent=tool_context.agent_name,
            tool=tool.name,
            args=_truncate(tool_args),
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        key = f"{tool_context.invocation_id}:{tool_context.function_call_id}"
        started = self._tool_start.pop(key, time.perf_counter())
        status = result.get("status", "n/a") if isinstance(result, dict) else "n/a"
        if status == "error":
            self.metrics[tool_context.invocation_id]["tool_errors"] += 1
        _log(
            self.logger,
            "tool_completed",
            invocation_id=tool_context.invocation_id,
            tool=tool.name,
            status=status,
            latency_ms=round((time.perf_counter() - started) * 1000),
            result_preview=_truncate(result),
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict]:
        self.metrics[tool_context.invocation_id]["tool_errors"] += 1
        _log(
            self.logger,
            "tool_error",
            level=logging.ERROR,
            invocation_id=tool_context.invocation_id,
            tool=tool.name,
            error=repr(error),
        )
        # Graceful degradation: hand the model a structured error to reason about.
        return {
            "status": "error",
            "error_message": f"Tool '{tool.name}' failed: {error}. Try different inputs or another approach.",
        }
