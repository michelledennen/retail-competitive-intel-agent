"""Distributed tracing with OpenTelemetry.

ADK already emits OpenTelemetry spans for every agent run, LLM call and tool
call (visible in the ``adk web`` **Trace** tab). This module adds:

* a project tracer (:func:`get_tracer`) used by ``ObservabilityPlugin`` to wrap
  every tool execution in a custom ``retail_intel.tool/<name>`` span with
  business attributes (tool name, agent, argument preview, status, latency);
* :func:`current_trace_ids` so every JSON log line carries ``trace_id`` /
  ``span_id`` -- you can jump from a log entry in Cloud Logging straight to
  the matching trace in Cloud Trace;
* optional export to **Google Cloud Trace** when ``ENABLE_CLOUD_TRACE=true``.
  ``opentelemetry-exporter-gcp-trace`` is imported lazily, so it is only
  needed where Cloud Trace is actually used. (``adk web --trace_to_cloud`` and
  ``adk deploy cloud_run --trace_to_cloud`` are equivalent built-in options.)
"""

from __future__ import annotations

import logging
import os
import threading

from opentelemetry import trace

TRACER_NAME = "retail_intel_agent"

logger = logging.getLogger("retail_intel_agent.telemetry")
_lock = threading.Lock()
_cloud_trace_configured = False


def get_tracer() -> trace.Tracer:
    """Returns the tracer for custom spans (resolves the current provider)."""
    return trace.get_tracer(TRACER_NAME)


def current_trace_ids() -> dict[str, str]:
    """Returns the active ``trace_id`` / ``span_id`` as hex strings (or {})."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx or not ctx.is_valid:
        return {}
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    ids = {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    if project:
        # Cloud Logging auto-links log entries to Cloud Trace via this field.
        ids["logging.googleapis.com/trace"] = f"projects/{project}/traces/{ids['trace_id']}"
    return ids


def configure_cloud_trace(force: bool = False) -> bool:
    """Adds a Cloud Trace exporter to the global tracer provider (idempotent).

    Called lazily on the first agent run so that ``adk web`` has already
    installed its own provider (which powers the Trace tab); we *add* a
    processor to it instead of replacing it.

    Args:
        force: Configure even if ``ENABLE_CLOUD_TRACE`` is not set (tests).

    Returns:
        True if a Cloud Trace exporter is active.
    """
    global _cloud_trace_configured
    enabled = os.getenv("ENABLE_CLOUD_TRACE", "false").lower() in ("1", "true", "yes")
    if not (enabled or force):
        return False
    with _lock:
        if _cloud_trace_configured:
            return True
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            logger.warning(
                "ENABLE_CLOUD_TRACE=true but opentelemetry-exporter-gcp-trace is not "
                "installed; run `pip install opentelemetry-exporter-gcp-trace`."
            )
            return False
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        exporter = CloudTraceSpanExporter(project_id=project) if project else CloudTraceSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _cloud_trace_configured = True
        logger.info("Cloud Trace export enabled")
        return True
