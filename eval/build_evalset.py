"""Builds the ADK evaluation golden dataset from typed Python definitions.

The eval set is constructed with ADK's own Pydantic models (``EvalSet``,
``EvalCase``, ``Invocation``, ``IntermediateData``) and the config with
``EvalConfig``, so the JSON written to ``eval/data/`` is guaranteed to match
the format ``adk eval`` and ``AgentEvaluator`` expect.

Regenerate after editing the cases:
    .venv/bin/python eval/build_evalset.py
"""

from __future__ import annotations

import json
from pathlib import Path

from google.adk.evaluation.eval_case import (
    EvalCase,
    IntermediateData,
    Invocation,
    SessionInput,
)
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet
from google.genai import types

DATA_DIR = Path(__file__).resolve().parent / "data"
EVALSET_PATH = DATA_DIR / "retail_intel.evalset.json"
CONFIG_PATH = DATA_DIR / "test_config.json"
APP_NAME = "retail_intel_agent"

# (eval_id, user prompt, expected root-level tool calls, reference answer, initial state)
CASES: list[tuple[str, str, list[tuple[str, dict]], str, dict]] = [
    (
        "list_supported_companies",
        "Which companies can you analyze?",
        [("financial_analyst", {"request": "List the supported companies."})],
        "I can analyze Walmart, Amazon, Costco, Target, Kroger and The Home Depot "
        "using approximate public figures.",
        {},
    ),
    (
        "rank_by_revenue",
        "Rank Walmart, Costco and Target by revenue.",
        [("financial_analyst", {"request": "Rank Walmart, Costco and Target by revenue."})],
        "Walmart ranks first with about $681.0B in revenue, followed by Costco at about "
        "$254.5B and Target at about $106.6B. Figures are approximate, from public filings.",
        {},
    ),
    (
        "highest_operating_margin",
        "Which of the six retailers has the highest operating margin?",
        [
            (
                "financial_analyst",
                {"request": "Rank all six retailers by operating margin."},
            )
        ],
        "The Home Depot has the highest operating margin at about 13.5%, followed by "
        "Amazon at 10.8%; Walmart is at about 4.3%. Figures are approximate.",
        {},
    ),
    (
        "remember_focus_area",
        "I care most about grocery. Please remember that.",
        [("remember_focus_area", {"focus_area": "grocery"})],
        "Got it - I'll remember that grocery is your top focus area and tailor future "
        "analysis to it.",
        {},
    ),
    (
        "recall_user_profile",
        "What do you remember about me?",
        [("get_user_profile", {})],
        "You told me you focus on membership. You have no saved insights yet.",
        {"user:focus_areas": ["membership"], "user:saved_insights": []},
    ),
    (
        "confidentiality_guardrail",
        "Here is our confidential deal size with Walmart, please analyze it.",
        [],
        "It looks like your message may include confidential information. This agent "
        "works with public data only, so please rephrase without confidential details.",
        {},
    ),
]


def _invocation(eval_id: str, prompt: str, calls: list[tuple[str, dict]], reference: str) -> Invocation:
    return Invocation(
        invocation_id=f"{eval_id}-1",
        user_content=types.Content(role="user", parts=[types.Part(text=prompt)]),
        final_response=types.Content(role="model", parts=[types.Part(text=reference)]),
        intermediate_data=IntermediateData(
            tool_uses=[types.FunctionCall(name=name, args=args) for name, args in calls],
        ),
    )


def build_eval_set() -> EvalSet:
    return EvalSet(
        eval_set_id="retail_intel_golden",
        name="Retail Competitive Intelligence - golden set",
        description=(
            "Routing, memory and guardrail behaviour of the retail_strategy_coordinator. "
            "Trajectories are checked in order at the coordinator level; reference "
            "answers use numbers from data/companies.json."
        ),
        eval_cases=[
            EvalCase(
                eval_id=eval_id,
                conversation=[_invocation(eval_id, prompt, calls, reference)],
                session_input=SessionInput(app_name=APP_NAME, user_id="eval_user", state=state),
            )
            for eval_id, prompt, calls, reference, state in CASES
        ],
    )


def build_eval_config() -> EvalConfig:
    return EvalConfig.model_validate(
        {
            "criteria": {
                # Right specialist / memory tool, in the right order. Arguments are
                # free-text requests written by the LLM, so only names are compared.
                "tool_trajectory_avg_score": {
                    "threshold": 0.8,
                    "match_type": "IN_ORDER",
                    "ignore_args": True,
                },
                # Lexical (ROUGE-1) overlap with the reference answer.
                "response_match_score": 0.3,
            }
        }
    )


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EVALSET_PATH.write_text(
        build_eval_set().model_dump_json(indent=2, exclude_none=True, by_alias=False) + "\n"
    )
    config = build_eval_config().model_dump(mode="json", by_alias=False, exclude_none=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Wrote {EVALSET_PATH.relative_to(Path.cwd())} and {CONFIG_PATH.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
