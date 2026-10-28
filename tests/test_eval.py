"""Automated evaluation suite.

* Offline (always run): the golden eval set and config are valid ADK
  ``EvalSet`` / ``EvalConfig`` files, stay in sync with
  ``eval/build_evalset.py``, only reference real tools, and the deterministic
  tools reproduce the golden values in ``eval/data/golden_tool_outputs.json``.
* Live (needs GOOGLE_API_KEY + ``pip install -r requirements-eval.txt``):
  ``AgentEvaluator`` runs every case against the real agent and enforces the
  thresholds in ``eval/data/test_config.json``.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet

from retail_intel_agent import agent as agent_module
from retail_intel_agent.tools import retail_tools

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "eval" / "data"
EVALSET = DATA / "retail_intel.evalset.json"
CONFIG = DATA / "test_config.json"
GOLDEN = json.loads((DATA / "golden_tool_outputs.json").read_text())


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_evalset", ROOT / "eval" / "build_evalset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root_tool_names() -> set[str]:
    return {getattr(t, "name", getattr(t, "__name__", "")) for t in agent_module.root_agent.tools}


def test_evalset_is_valid_adk_format():
    eval_set = EvalSet.model_validate_json(EVALSET.read_text())
    assert len(eval_set.eval_cases) >= 5
    ids = [c.eval_id for c in eval_set.eval_cases]
    assert len(ids) == len(set(ids)), "eval ids must be unique"
    for case in eval_set.eval_cases:
        assert case.conversation, case.eval_id
        for inv in case.conversation:
            assert inv.final_response and inv.final_response.parts[0].text
            for call in inv.intermediate_data.tool_uses:
                assert call.name in _root_tool_names(), f"{case.eval_id}: unknown tool {call.name}"


def test_evalset_matches_builder():
    builder = _load_builder()
    on_disk = EvalSet.model_validate_json(EVALSET.read_text())
    assert on_disk == builder.build_eval_set(), "Run `python eval/build_evalset.py` to regenerate."


def test_eval_config_is_valid():
    config = EvalConfig.model_validate(json.loads(CONFIG.read_text()))
    assert "tool_trajectory_avg_score" in config.criteria
    assert "response_match_score" in config.criteria


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["id"])
def test_golden_tool_outputs(case):
    fn = getattr(retail_tools, case["tool"])
    args = dict(case["args"])
    if case["tool"] == "compare_companies":
        args["tool_context"] = SimpleNamespace(state={})
    result = fn(**args)
    expect = case["expect"]
    assert result["status"] == expect["status"]
    if "ranking_keys" in expect:
        assert [r["key"] for r in result["ranking"]] == expect["ranking_keys"]
        assert result["ranking"][0]["value"] == pytest.approx(expect["top_value"])
    if "profile" in expect:
        for key, value in expect["profile"].items():
            assert result["profile"][key] == value
    if expect.get("has_recovery"):
        assert result["recovery"]


def _eval_deps_available() -> bool:
    try:
        import google.adk.evaluation.metric_evaluator_registry  # noqa: F401
        import pandas  # noqa: F401
        import rouge_score  # noqa: F401
        import tabulate  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")),
    reason="Live evaluation requires GOOGLE_API_KEY",
)
@pytest.mark.skipif(not _eval_deps_available(), reason="pip install -r requirements-eval.txt")
@pytest.mark.asyncio
async def test_agent_evaluator_golden_set():
    from google.adk.evaluation.agent_evaluator import AgentEvaluator

    await AgentEvaluator.evaluate(
        agent_module="retail_intel_agent",
        eval_dataset_file_path_or_dir=str(EVALSET),
        num_runs=1,
    )
