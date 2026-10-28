"""Tests for agent wiring, callbacks and observability (no API key needed)."""

import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.models.llm_request import LlmRequest
from google.adk.tools import FunctionTool
from google.genai import types

from retail_intel_agent import agent as agent_module
from retail_intel_agent import telemetry
from retail_intel_agent.observability import ObservabilityPlugin
from retail_intel_agent.tools.schemas import BriefReview


def test_app_exposes_root_agent_and_plugin():
    app = agent_module.app
    assert app.root_agent is agent_module.root_agent
    assert any(isinstance(p, ObservabilityPlugin) for p in app.plugins)
    assert app.events_compaction_config is not None


def test_root_agent_tools():
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in agent_module.root_agent.tools}
    assert {
        "financial_analyst",
        "market_researcher",
        "competitive_report_pipeline",
        "remember_focus_area",
        "save_insight",
        "clear_user_memory",
        "get_user_profile",
        "load_memory",
    } <= names


@pytest.mark.parametrize("tool_name", ["save_insight", "clear_user_memory"])
def test_high_stakes_tools_require_human_confirmation(tool_name):
    tool = next(t for t in agent_module.root_agent.tools if getattr(t, "name", None) == tool_name)
    assert isinstance(tool, FunctionTool)
    assert tool._require_confirmation is True


@pytest.mark.asyncio
async def test_confirmation_tool_pauses_until_approved():
    """Without a ToolConfirmation, ADK requests approval instead of running the tool."""
    tool = agent_module.clear_user_memory_tool
    requested = {}
    ctx = SimpleNamespace(
        state={"user:focus_areas": ["grocery"]},
        tool_confirmation=None,
        actions=SimpleNamespace(skip_summarization=False),
        request_confirmation=lambda hint=None, payload=None: requested.update(hint=hint),
    )
    result = await tool.run_async(args={}, tool_context=ctx)
    assert "requires confirmation" in result["error"]
    assert requested["hint"]
    assert ctx.state["user:focus_areas"] == ["grocery"]  # nothing deleted yet

    ctx.tool_confirmation = SimpleNamespace(confirmed=True)
    result = await tool.run_async(args={}, tool_context=ctx)
    assert result["status"] == "success"
    assert ctx.state["user:focus_areas"] == []


def test_pipeline_structure():
    pipeline = agent_module.competitive_report_pipeline
    assert isinstance(pipeline, SequentialAgent)
    fanout, writer, reviewer = pipeline.sub_agents
    assert isinstance(fanout, ParallelAgent)
    assert isinstance(writer, LlmAgent)
    output_keys = {a.output_key for a in fanout.sub_agents}
    assert output_keys == {"financial_findings", "market_news"}
    assert "{financial_findings?}" in writer.instruction
    assert "{market_news?}" in writer.instruction
    assert writer.output_key == "strategy_brief"


def test_brief_quality_reviewer_self_evaluates_with_schema():
    reviewer = agent_module.competitive_report_pipeline.sub_agents[-1]
    assert reviewer.name == "brief_quality_reviewer"
    assert reviewer.output_schema is BriefReview
    assert reviewer.output_key == "brief_review"
    assert "{strategy_brief?}" in reviewer.instruction
    assert "{financial_findings?}" in reviewer.instruction
    review = BriefReview.model_validate(
        {
            "verdict": "revised",
            "checks": {"numbers_grounded": False, "all_sections_present": True, "disclaimer_present": True},
            "issues_found": ["Removed an ungrounded figure"],
            "final_brief": "**Headline** ...",
        }
    )
    assert review.verdict == "revised"


def test_coordinator_constitution_sections():
    text = agent_module.root_agent.static_instruction
    for heading in ("Identity and mission", "Domain knowledge", "Hard constraints", "Error handling"):
        assert heading in text


def test_static_instructions_have_no_state_placeholders():
    """Static (system) instructions must be identical every turn so the
    context cache stays aligned; per-turn state lives in `instruction`."""
    agents = [
        agent_module.root_agent,
        *agent_module.competitive_report_pipeline.sub_agents[1:],  # writer, reviewer
    ]
    for agent in agents:
        assert agent.static_instruction
        assert "{" not in agent.static_instruction, agent.name
    assert "{user:focus_areas?}" in agent_module.root_agent.instruction


def _request(text: str) -> LlmRequest:
    return LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text=text)])])


def test_guardrail_blocks_confidential():
    ctx = SimpleNamespace(state={})
    resp = agent_module.confidentiality_guardrail(ctx, _request("Here is our CONFIDENTIAL deal size"))
    assert resp is not None
    assert "public data only" in resp.content.parts[0].text
    assert ctx.state["temp:guardrail_triggered"] is True


def test_guardrail_allows_normal_question():
    ctx = SimpleNamespace(state={})
    assert agent_module.confidentiality_guardrail(ctx, _request("Compare Walmart and Target")) is None


def test_initialize_state_sets_defaults_without_overwriting():
    ctx = SimpleNamespace(state={"user:focus_areas": ["grocery"]})
    agent_module.initialize_state(ctx)
    assert ctx.state["user:focus_areas"] == ["grocery"]
    assert ctx.state["user:saved_insights"] == []


@pytest.mark.asyncio
async def test_save_session_to_memory_runs_in_background():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_save():
        started.set()
        await release.wait()

    ctx = SimpleNamespace(add_session_to_memory=slow_save)
    # Returns immediately even though the save has not finished.
    assert await agent_module.save_session_to_memory(ctx) is None
    assert len(agent_module._BACKGROUND_TASKS) == 1  # strong reference kept
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not agent_module._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_background_memory_failure_is_logged_not_raised(caplog):
    async def failing_save():
        raise RuntimeError("memory down")

    ctx = SimpleNamespace(add_session_to_memory=failing_save)
    with caplog.at_level(logging.WARNING, logger="retail_intel_agent.agent"):
        await agent_module.save_session_to_memory(ctx)
        for _ in range(3):
            await asyncio.sleep(0)
    assert any("memory_save_failed" in r.getMessage() for r in caplog.records)


def _tool_ctx(inv: str, fc: str):
    return SimpleNamespace(invocation_id=inv, function_call_id=fc, agent_name="financial_analyst")


@pytest.mark.asyncio
async def test_observability_logs_intent_and_outcome_with_correlation_id(caplog):
    plugin = ObservabilityPlugin()
    plugin.logger.propagate = True
    tool = SimpleNamespace(name="compare_companies")
    tctx = _tool_ctx("inv-1", "fc-1")
    with caplog.at_level(logging.INFO, logger=plugin.logger.name):
        await plugin.before_tool_callback(tool=tool, tool_args={"x": 1}, tool_context=tctx)
        await plugin.after_tool_callback(
            tool=tool, tool_args={"x": 1}, tool_context=tctx, result={"status": "error"}
        )
    m = plugin.metrics["inv-1"]
    assert m["tool_calls"] == 1
    assert m["tool_errors"] == 1
    assert m["tools_used"] == ["compare_companies"]
    records = {r.getMessage(): r.fields for r in caplog.records}
    intent, outcome = records["intent:tool"], records["outcome:tool"]
    assert intent["event"] == "intent" and outcome["event"] == "outcome"
    assert intent["correlation_id"] == outcome["correlation_id"] == "fc-1"
    assert intent["args"] == '{"x": 1}'
    assert outcome["status"] == "error"
    assert "latency_ms" in outcome


@pytest.mark.asyncio
async def test_observability_creates_custom_tool_span():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    plugin = ObservabilityPlugin()
    tool = SimpleNamespace(name="get_company_profile")
    tctx = _tool_ctx("inv-span", "fc-span")
    with patch.object(telemetry, "get_tracer", lambda: provider.get_tracer("test")):
        await plugin.before_tool_callback(tool=tool, tool_args={"company": "WMT"}, tool_context=tctx)
        await plugin.after_tool_callback(
            tool=tool, tool_args={}, tool_context=tctx, result={"status": "success"}
        )
    (span,) = exporter.get_finished_spans()
    assert span.name == "retail_intel.tool/get_company_profile"
    assert span.attributes["retail_intel.tool.status"] == "success"
    assert span.attributes["retail_intel.correlation_id"] == "fc-span"


def test_json_logs_include_trace_ids():
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("unit"):
        ids = telemetry.current_trace_ids()
    assert len(ids["trace_id"]) == 32 and len(ids["span_id"]) == 16


@pytest.mark.asyncio
async def test_observability_plugin_handles_tool_exception():
    plugin = ObservabilityPlugin()
    tool = SimpleNamespace(name="get_company_profile")
    tctx = _tool_ctx("inv-2", "fc-2")
    await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=tctx)
    result = await plugin.on_tool_error_callback(
        tool=tool, tool_args={}, tool_context=tctx, error=ValueError("boom")
    )
    assert result["status"] == "error"
    assert "boom" in result["error_message"]
    assert result["recovery"]


@pytest.mark.skipif(
    not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")),
    reason="Live model test requires GOOGLE_API_KEY",
)
@pytest.mark.asyncio
async def test_live_end_to_end_ranking():
    from google.adk.runners import InMemoryRunner

    runner = InMemoryRunner(app=agent_module.app)
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="ci")
    message = types.Content(
        role="user",
        parts=[types.Part(text="Rank Walmart, Costco and Target by revenue.")],
    )
    final_text = ""
    async for event in runner.run_async(user_id="ci", session_id=session.id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)
    assert "walmart" in final_text.lower()


def test_search_fallback_on_quota_error():
    ctx = SimpleNamespace(state={})
    resp = agent_module.search_fallback_on_error(
        ctx, _request("news"), RuntimeError("429 RESOURCE_EXHAUSTED search_request_free")
    )
    assert resp is not None
    assert "temporarily unavailable" in resp.content.parts[0].text
    assert ctx.state["temp:search_unavailable"] is True


def test_search_fallback_ignores_other_errors():
    ctx = SimpleNamespace(state={})
    assert agent_module.search_fallback_on_error(ctx, _request("x"), ValueError("bad")) is None
