"""Tests for agent wiring, callbacks and observability (no API key needed)."""

import logging
import os
from types import SimpleNamespace

import pytest
from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from retail_intel_agent import agent as agent_module
from retail_intel_agent.observability import ObservabilityPlugin


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
        "get_user_profile",
        "load_memory",
    } <= names


def test_pipeline_structure():
    pipeline = agent_module.competitive_report_pipeline
    assert isinstance(pipeline, SequentialAgent)
    fanout, writer = pipeline.sub_agents
    assert isinstance(fanout, ParallelAgent)
    assert isinstance(writer, LlmAgent)
    output_keys = {a.output_key for a in fanout.sub_agents}
    assert output_keys == {"financial_findings", "market_news"}
    assert "{financial_findings?}" in writer.instruction
    assert "{market_news?}" in writer.instruction


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
async def test_observability_plugin_tracks_tool_metrics(caplog):
    plugin = ObservabilityPlugin()
    plugin.logger.propagate = True
    tool = SimpleNamespace(name="compare_companies")
    tctx = SimpleNamespace(invocation_id="inv-1", function_call_id="fc-1", agent_name="a")
    with caplog.at_level(logging.INFO, logger=plugin.logger.name):
        await plugin.before_tool_callback(tool=tool, tool_args={"x": 1}, tool_context=tctx)
        await plugin.after_tool_callback(
            tool=tool, tool_args={"x": 1}, tool_context=tctx, result={"status": "error"}
        )
    m = plugin.metrics["inv-1"]
    assert m["tool_calls"] == 1
    assert m["tool_errors"] == 1
    assert m["tools_used"] == ["compare_companies"]
    assert any("tool_completed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_observability_plugin_handles_tool_exception():
    plugin = ObservabilityPlugin()
    tool = SimpleNamespace(name="get_company_profile")
    tctx = SimpleNamespace(invocation_id="inv-2", function_call_id="fc-2", agent_name="a")
    result = await plugin.on_tool_error_callback(
        tool=tool, tool_args={}, tool_context=tctx, error=ValueError("boom")
    )
    assert result["status"] == "error"
    assert "boom" in result["error_message"]


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
