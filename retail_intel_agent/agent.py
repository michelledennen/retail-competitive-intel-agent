"""Retail Competitive Intelligence Agent -- multi-agent system built with ADK.

Architecture
------------
retail_strategy_coordinator (LlmAgent, root, PRO_MODEL)
├── tool: financial_analyst          (AgentTool -> LlmAgent [FAST_MODEL] + 4 function tools)
├── tool: market_researcher          (AgentTool -> LlmAgent [FAST_MODEL] + Google Search)
├── tool: competitive_report_pipeline (AgentTool -> SequentialAgent)
│       ├── research_fanout (ParallelAgent)
│       │     ├── financial_snapshot  [FAST] -> state["financial_findings"]
│       │     └── news_scan           [FAST] -> state["market_news"]
│       ├── strategy_writer           [PRO]  -> state["strategy_brief"]
│       └── brief_quality_reviewer    [PRO]  -> state["brief_review"] (self-evaluation,
│                                               structured BriefReview output)
├── tool: remember_focus_area / get_user_profile (user-scoped state)
├── tool: save_insight / clear_user_memory       (human-in-the-loop confirmation)
└── tool: load_memory                 (long-term memory across sessions)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import AgentTool, FunctionTool, google_search, load_memory
from google.genai import types

from . import config, prompts
from .observability import ObservabilityPlugin
from .tools import (
    build_comparison_table,
    clear_user_memory,
    compare_companies,
    get_company_profile,
    get_user_profile,
    list_supported_companies,
    remember_focus_area,
    save_insight,
)
from .tools.retail_tools import STATE_FOCUS_AREAS, STATE_SAVED_INSIGHTS
from .tools.schemas import BriefReview

logger = logging.getLogger("retail_intel_agent.agent")

# Resilience: transient server errors (model overloaded / 5xx) are retried with
# exponential backoff. 429 quota errors are NOT retried; they are handled by
# graceful-degradation callbacks instead.
MODEL_RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=4,
    initial_delay=2.0,
    exp_base=2.0,
    max_delay=20.0,
    http_status_codes=[500, 502, 503, 504],
)


def _model(name: str) -> Gemini:
    """Builds a Gemini model with retry/backoff for transient failures."""
    return Gemini(model=name, retry_options=MODEL_RETRY_OPTIONS)


FINANCIAL_TOOLS = [
    list_supported_companies,
    get_company_profile,
    compare_companies,
    build_comparison_table,
]

# Human-in-the-loop: persistent / destructive writes pause for user approval
# (ADK emits an adk_request_confirmation event; `adk web` shows Approve/Reject).
save_insight_tool = FunctionTool(save_insight, require_confirmation=True)
clear_user_memory_tool = FunctionTool(clear_user_memory, require_confirmation=True)

# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------

_CONFIDENTIAL_PATTERN = re.compile(
    r"\b(confidential|internal[- ]only|under nda|privileged|do not share|"
    r"contract value|deal size|pricing proposal)\b",
    re.IGNORECASE,
)

# Strong references to in-flight background tasks so they are not garbage
# collected before they finish (see asyncio.create_task docs).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


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


def _log_memory_task_result(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        logger.warning("memory_save_cancelled")
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("memory_save_failed: %r", exc)


async def save_session_to_memory(callback_context: CallbackContext) -> Optional[types.Content]:
    """After each turn, store the session in long-term memory -- asynchronously.

    The (potentially slow) memory write runs as a background asyncio task so
    it never delays the user's response. Failures are logged, never raised.
    """
    try:
        task = asyncio.create_task(
            callback_context.add_session_to_memory(), name="retail_intel.memory_save"
        )
    except Exception as exc:  # e.g. no memory service configured
        logger.warning("memory_save_not_scheduled: %r", exc)
        return None
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_log_memory_task_result)
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
        model=_model(config.FAST_MODEL),  # tool-calling data lookups: fast + cheap
        description="Analyzes and compares public financial metrics of major US retailers.",
        instruction=instruction,
        tools=FINANCIAL_TOOLS,
        output_key=output_key,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
    )


SEARCH_UNAVAILABLE_MESSAGE = (
    "Live web search is temporarily unavailable (quota or rate limit reached). "
    "No recent news could be retrieved. Downstream steps should rely on the "
    "built-in public financial dataset and clearly note that recent news was "
    "not included."
)


def search_fallback_on_error(
    callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
) -> Optional[LlmResponse]:
    """Graceful degradation: if web search hits a quota/rate limit, return a
    clear notice instead of failing the whole pipeline."""
    text = repr(error)
    if "RESOURCE_EXHAUSTED" in text or "429" in text or "UNAVAILABLE" in text:
        callback_context.state["temp:search_unavailable"] = True
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=SEARCH_UNAVAILABLE_MESSAGE)])
        )
    return None


def _research_agent(name: str, instruction: str, output_key: str | None = None) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=_model(config.FAST_MODEL),  # search + summarize: fast + cheap
        description="Searches the web for recent public news about retailers.",
        instruction=instruction,
        tools=[google_search],
        output_key=output_key,
        on_model_error_callback=search_fallback_on_error,
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
    model=_model(config.PRO_MODEL),  # synthesis + reasoning
    description="Writes an executive competitive brief with a SWOT and talking points.",
    static_instruction=prompts.STRATEGY_WRITER_STATIC_INSTRUCTION,
    instruction=prompts.STRATEGY_WRITER_DYNAMIC_INSTRUCTION,
    output_key="strategy_brief",
    generate_content_config=types.GenerateContentConfig(temperature=0.4),
)

# Self-evaluation: audits the draft against the source numbers and returns a
# structured, validated BriefReview (verdict, checks, issues, final_brief).
brief_quality_reviewer = LlmAgent(
    name="brief_quality_reviewer",
    model=_model(config.PRO_MODEL),
    description="Checks the brief is grounded, complete and disclaimed; fixes it if not.",
    static_instruction=prompts.BRIEF_QUALITY_REVIEWER_STATIC_INSTRUCTION,
    instruction=prompts.BRIEF_QUALITY_REVIEWER_DYNAMIC_INSTRUCTION,
    output_schema=BriefReview,
    output_key="brief_review",
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
)

competitive_report_pipeline = SequentialAgent(
    name="competitive_report_pipeline",
    description=(
        "Produces a full competitive brief: gathers financials and news in "
        "parallel, writes a SWOT and talking points, then self-reviews the "
        "brief for grounding and completeness. Input should list the "
        "companies and focus areas."
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
        brief_quality_reviewer,
    ],
)

# --------------------------------------------------------------------------
# Root agent + App
# --------------------------------------------------------------------------

root_agent = LlmAgent(
    name="retail_strategy_coordinator",
    model=_model(config.PRO_MODEL),  # routing + reasoning
    description="Coordinates competitive analysis of Walmart vs. top US retailers.",
    static_instruction=prompts.COORDINATOR_STATIC_INSTRUCTION,
    instruction=prompts.COORDINATOR_DYNAMIC_INSTRUCTION,
    tools=[
        AgentTool(agent=financial_analyst),
        AgentTool(agent=market_researcher),
        AgentTool(agent=competitive_report_pipeline),
        remember_focus_area,
        get_user_profile,
        save_insight_tool,
        clear_user_memory_tool,
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
