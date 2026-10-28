"""Retail Competitive Intelligence Agent -- multi-agent system built with ADK.

Architecture
------------
retail_strategy_coordinator (LlmAgent, root)
├── tool: financial_analyst          (AgentTool -> LlmAgent + 4 function tools)
├── tool: market_researcher          (AgentTool -> LlmAgent + Google Search)
├── tool: competitive_report_pipeline (AgentTool -> SequentialAgent)
│       ├── research_fanout (ParallelAgent)
│       │     ├── financial_snapshot  -> state["financial_findings"]
│       │     └── news_scan           -> state["market_news"]
│       └── strategy_writer           (reads both from state)
├── tool: remember_focus_area / save_insight / get_user_profile (user-scoped state)
└── tool: load_memory                 (long-term memory across sessions)
"""

from __future__ import annotations

import re
from typing import Optional

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import AgentTool, google_search, load_memory
from google.genai import types

from . import config, prompts
from .observability import ObservabilityPlugin
from .tools import (
    build_comparison_table,
    compare_companies,
    get_company_profile,
    get_user_profile,
    list_supported_companies,
    remember_focus_area,
    save_insight,
)
from .tools.retail_tools import STATE_FOCUS_AREAS, STATE_SAVED_INSIGHTS

FINANCIAL_TOOLS = [
    list_supported_companies,
    get_company_profile,
    compare_companies,
    build_comparison_table,
]

# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------

_CONFIDENTIAL_PATTERN = re.compile(
    r"\b(confidential|internal[- ]only|under nda|privileged|do not share|"
    r"contract value|deal size|pricing proposal)\b",
    re.IGNORECASE,
)


def initialize_state(callback_context: CallbackContext) -> Optional[types.Content]:
    """Ensures memory keys exist so instruction templates always resolve."""
    state = callback_context.state
    for key, default in (
        (STATE_FOCUS_AREAS, []),
        (STATE_SAVED_INSIGHTS, []),
    ):
        if key not in state:
            state[key] = default
    return None


async def save_session_to_memory(callback_context: CallbackContext) -> Optional[types.Content]:
    """After each turn, store the session in long-term memory for later recall."""
    try:
        await callback_context.add_session_to_memory()
    except Exception:  # memory service may be unavailable; never break the chat
        pass
    return None


def confidentiality_guardrail(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> Optional[LlmResponse]:
    """Input guardrail: blocks requests that appear to contain confidential info."""
    last_user_text = ""
    for content in reversed(llm_request.contents or []):
        if content.role == "user" and content.parts:
            last_user_text = " ".join(p.text or "" for p in content.parts if p.text)
            if last_user_text:
                break
    if last_user_text and _CONFIDENTIAL_PATTERN.search(last_user_text):
        callback_context.state["temp:guardrail_triggered"] = True
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            "It looks like your message may include confidential "
                            "information. This agent works with public data only, "
                            "so please rephrase without confidential details and "
                            "I'll be glad to help."
                        )
                    )
                ],
            )
        )
    return None


# --------------------------------------------------------------------------
# Specialist agents
# --------------------------------------------------------------------------


def _financial_agent(name: str, instruction: str, output_key: str | None = None) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=config.MODEL,
        description="Analyzes and compares public financial metrics of major US retailers.",
        instruction=instruction,
        tools=FINANCIAL_TOOLS,
        output_key=output_key,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
    )


def _research_agent(name: str, instruction: str, output_key: str | None = None) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=config.MODEL,
        description="Searches the web for recent public news about retailers.",
        instruction=instruction,
        tools=[google_search],
        output_key=output_key,
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
    )


financial_analyst = _financial_agent(
    "financial_analyst", prompts.FINANCIAL_ANALYST_INSTRUCTION
)
market_researcher = _research_agent(
    "market_researcher", prompts.MARKET_RESEARCHER_INSTRUCTION
)

strategy_writer = LlmAgent(
    name="strategy_writer",
    model=config.MODEL,
    description="Writes an executive competitive brief with a SWOT and talking points.",
    instruction=prompts.STRATEGY_WRITER_INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.4),
)

competitive_report_pipeline = SequentialAgent(
    name="competitive_report_pipeline",
    description=(
        "Produces a full competitive brief: gathers financials and news in "
        "parallel, then writes a SWOT and talking points. Input should list "
        "the companies and focus areas."
    ),
    sub_agents=[
        ParallelAgent(
            name="research_fanout",
            description="Gathers financial data and recent news at the same time.",
            sub_agents=[
                _financial_agent(
                    "financial_snapshot",
                    prompts.FINANCIAL_SNAPSHOT_INSTRUCTION,
                    output_key="financial_findings",
                ),
                _research_agent(
                    "news_scan",
                    prompts.NEWS_SCAN_INSTRUCTION,
                    output_key="market_news",
                ),
            ],
        ),
        strategy_writer,
    ],
)

# --------------------------------------------------------------------------
# Root agent + App
# --------------------------------------------------------------------------

root_agent = LlmAgent(
    name="retail_strategy_coordinator",
    model=config.MODEL,
    description="Coordinates competitive analysis of Walmart vs. top US retailers.",
    instruction=prompts.COORDINATOR_INSTRUCTION,
    tools=[
        AgentTool(agent=financial_analyst),
        AgentTool(agent=market_researcher),
        AgentTool(agent=competitive_report_pipeline),
        remember_focus_area,
        save_insight,
        get_user_profile,
        load_memory,
    ],
    before_agent_callback=initialize_state,
    after_agent_callback=save_session_to_memory,
    before_model_callback=confidentiality_guardrail,
    generate_content_config=types.GenerateContentConfig(temperature=0.3),
)

app = App(
    name=config.APP_NAME,
    root_agent=root_agent,
    plugins=[ObservabilityPlugin()],
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=config.COMPACTION_INTERVAL,
        overlap_size=config.COMPACTION_OVERLAP,
    ),
)
