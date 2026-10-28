"""Observability for the Retail Competitive Intelligence Agent.

Four layers, all implemented in one ADK ``BasePlugin`` so they apply to every
agent, sub-agent and tool without touching business logic:

1. **Structured JSON logs** -- one JSON object per line (Cloud Logging parses
   these natively). Every payload passes through :mod:`.pii` redaction.
2. **Intent vs. outcome capture** -- before each run / agent / model call /
   tool call we log an ``intent`` record (what is about to happen and with
   which inputs); afterwards an ``outcome`` record (status, result preview,
   latency, tokens). Both share a ``correlation_id`` so you can pair them and
   spot gaps ("intended to call X, but the outcome was an error").
3. **Per-run metrics** -- model calls, tool calls, token usage, errors and
   latency aggregated per invocation and emitted in the run ``outcome``.
4. **Distributed tracing** -- ADK emits OpenTelemetry spans automatically; we
   add a custom span around every tool execution (see :mod:`.telemetry`) and
   stamp ``trace_id`` / ``span_id`` on every log line.

The plugin also implements graceful tool error handling: if a tool raises,
the agent receives a structured error with recovery instructions instead of
crashing.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import defaultdict
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry.trace import Status, StatusCode

from . import telemetry
from .pii import redact, redact_obj

LOGGER_NAME = "retail_intel_agent"

# Identifier fields are never redacted (hex trace ids can look like card numbers).
_NO_REDACT = frozenset(
    {"trace_id", "span_id", "logging.googleapis.com/trace", "correlation_id", "invocation_id"}
)


class _JsonFormatter(logging.Formatter):
    """Renders a record as one JSON line with PII redacted and trace ids linked."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "severity": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        payload.update(telemetry.current_trace_ids())
        extra = getattr(record, "fields", None)
        if extra:
            for key, value in extra.items():
                payload[key] = value if key in _NO_REDACT else redact_obj(value)
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
    text = redact(text)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


class ObservabilityPlugin(BasePlugin):
    """Logs intent/outcome for every step, traces tools and aggregates metrics."""

    def __init__(self, name: str = "observability_plugin") -> None:
        super().__init__(name=name)
        self.logger = configure_logging()
        # key -> (start_time, correlation_id)
        self._run_start: dict[str, tuple[float, str]] = {}
        self._agent_start: dict[str, tuple[float, str]] = {}
        self._model_start: dict[str, tuple[float, str]] = {}
        self._tool_start: dict[str, tuple[float, str]] = {}
        self._tool_spans: dict[str, Any] = {}
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

    # ---- helpers -------------------------------------------------------
    def _intent(self, step: str, correlation_id: str, **fields: Any) -> None:
        _log(self.logger, f"intent:{step}", event="intent", step=step,
             correlation_id=correlation_id, **fields)

    def _outcome(
        self, step: str, correlation_id: str, level: int = logging.INFO, **fields: Any
    ) -> None:
        _log(self.logger, f"outcome:{step}", level=level, event="outcome", step=step,
             correlation_id=correlation_id, **fields)

    # ---- run lifecycle -------------------------------------------------
    async def before_run_callback(self, *, invocation_context: InvocationContext) -> None:
        telemetry.configure_cloud_trace()  # no-op unless ENABLE_CLOUD_TRACE=true
        inv = invocation_context.invocation_id
        cid = _new_id()
        self._run_start[inv] = (time.perf_counter(), cid)
        user_text = ""
        msg = getattr(invocation_context, "user_content", None)
        if msg and msg.parts:
            user_text = " ".join(p.text or "" for p in msg.parts if p.text)
        self._intent(
            "run",
            cid,
            invocation_id=inv,
            session_id=invocation_context.session.id,
            user_id=invocation_context.user_id,
            goal=_truncate(user_text, 200),
        )
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        inv = invocation_context.invocation_id
        started, cid = self._run_start.pop(inv, (time.perf_counter(), _new_id()))
        summary = dict(self.metrics.pop(inv, {}))
        errors = summary.get("tool_errors", 0) + summary.get("model_errors", 0)
        self._outcome(
            "run",
            cid,
            invocation_id=inv,
            status="error" if errors else "success",
            latency_ms=_ms_since(started),
            **summary,
        )

    # ---- agents --------------------------------------------------------
    async def before_agent_callback(self, *, agent, callback_context: CallbackContext):
        inv = callback_context.invocation_id
        cid = _new_id()
        self._agent_start[f"{inv}:{agent.name}"] = (time.perf_counter(), cid)
        self.metrics[inv]["agents_invoked"].append(agent.name)
        self._intent(
            "agent",
            cid,
            invocation_id=inv,
            agent=agent.name,
            purpose=_truncate(getattr(agent, "description", "") or "", 160),
        )
        return None

    async def after_agent_callback(self, *, agent, callback_context: CallbackContext):
        inv = callback_context.invocation_id
        started, cid = self._agent_start.pop(f"{inv}:{agent.name}", (time.perf_counter(), _new_id()))
        self._outcome(
            "agent", cid, invocation_id=inv, agent=agent.name, status="success",
            latency_ms=_ms_since(started),
        )
        return None

    # ---- models --------------------------------------------------------
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        inv = callback_context.invocation_id
        cid = _new_id()
        self._model_start[f"{inv}:{callback_context.agent_name}"] = (time.perf_counter(), cid)
        self.metrics[inv]["model_calls"] += 1
        self._intent(
            "model",
            cid,
            invocation_id=inv,
            agent=callback_context.agent_name,
            model=llm_request.model,
            num_contents=len(llm_request.contents or []),
            tools_offered=sorted((llm_request.tools_dict or {}).keys()),
        )
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        inv = callback_context.invocation_id
        key = f"{inv}:{callback_context.agent_name}"
        started, cid = self._model_start.pop(key, (time.perf_counter(), _new_id()))
        m = self.metrics[inv]
        usage = llm_response.usage_metadata
        prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (usage.candidates_token_count or 0) if usage else 0
        m["prompt_tokens"] += prompt_tokens
        m["output_tokens"] += output_tokens
        calls = []
        if llm_response.content and llm_response.content.parts:
            calls = [p.function_call.name for p in llm_response.content.parts if p.function_call]
        self._outcome(
            "model",
            cid,
            invocation_id=inv,
            agent=callback_context.agent_name,
            status="error" if llm_response.error_code else "success",
            decision="tool_call" if calls else "respond",
            requested_tools=calls,
            latency_ms=_ms_since(started),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            error_code=llm_response.error_code,
        )
        return None

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
    ) -> Optional[LlmResponse]:
        inv = callback_context.invocation_id
        key = f"{inv}:{callback_context.agent_name}"
        started, cid = self._model_start.pop(key, (time.perf_counter(), _new_id()))
        self.metrics[inv]["model_errors"] += 1
        self._outcome(
            "model",
            cid,
            level=logging.ERROR,
            invocation_id=inv,
            agent=callback_context.agent_name,
            status="error",
            latency_ms=_ms_since(started),
            error=repr(error),
        )
        return None  # let ADK surface the error

    # ---- tools ---------------------------------------------------------
    @staticmethod
    def _tool_key(tool_context: ToolContext) -> str:
        return f"{tool_context.invocation_id}:{tool_context.function_call_id}"

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict]:
        key = self._tool_key(tool_context)
        cid = tool_context.function_call_id or _new_id()
        self._tool_start[key] = (time.perf_counter(), cid)
        m = self.metrics[tool_context.invocation_id]
        m["tool_calls"] += 1
        m["tools_used"].append(tool.name)
        # Custom span: child of ADK's execute_tool span.
        span = telemetry.get_tracer().start_span(
            f"retail_intel.tool/{tool.name}",
            attributes={
                "retail_intel.tool.name": tool.name,
                "retail_intel.agent": tool_context.agent_name or "",
                "retail_intel.correlation_id": cid,
                "retail_intel.tool.args": _truncate(tool_args, 200),
            },
        )
        self._tool_spans[key] = span
        self._intent(
            "tool",
            cid,
            invocation_id=tool_context.invocation_id,
            agent=tool_context.agent_name,
            tool=tool.name,
            args=_truncate(tool_args),
        )
        return None

    def _end_span(self, key: str, status: str, latency_ms: int, error: Exception | None = None):
        span = self._tool_spans.pop(key, None)
        if span is None:
            return
        span.set_attribute("retail_intel.tool.status", status)
        span.set_attribute("retail_intel.tool.latency_ms", latency_ms)
        if error is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
        elif status == "error":
            span.set_status(Status(StatusCode.ERROR, "tool returned status=error"))
        span.end()

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        key = self._tool_key(tool_context)
        started, cid = self._tool_start.pop(key, (time.perf_counter(), _new_id()))
        status = "n/a"
        if isinstance(result, dict):
            status = result.get("status") or ("error" if "error" in result else "success")
        if status == "error":
            self.metrics[tool_context.invocation_id]["tool_errors"] += 1
        latency = _ms_since(started)
        self._end_span(key, status, latency)
        self._outcome(
            "tool",
            cid,
            level=logging.WARNING if status == "error" else logging.INFO,
            invocation_id=tool_context.invocation_id,
            agent=tool_context.agent_name,
            tool=tool.name,
            status=status,
            latency_ms=latency,
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
        key = self._tool_key(tool_context)
        started, cid = self._tool_start.pop(key, (time.perf_counter(), _new_id()))
        self.metrics[tool_context.invocation_id]["tool_errors"] += 1
        latency = _ms_since(started)
        self._end_span(key, "error", latency, error)
        self._outcome(
            "tool",
            cid,
            level=logging.ERROR,
            invocation_id=tool_context.invocation_id,
            agent=getattr(tool_context, "agent_name", None),
            tool=tool.name,
            status="error",
            latency_ms=latency,
            error=repr(error),
        )
        # Graceful degradation: hand the model a structured error to reason about.
        return {
            "status": "error",
            "error_message": f"Tool '{tool.name}' failed unexpectedly: {error}.",
            "recovery": (
                "Do not retry with identical arguments. Check the argument types "
                "against the tool schema, call list_supported_companies if a "
                "company name may be wrong, or answer with the data you already "
                "have and tell the user this step failed."
            ),
        }
