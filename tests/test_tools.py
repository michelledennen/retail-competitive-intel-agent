"""Unit tests for the function tools (no API key or network needed)."""

from types import SimpleNamespace

import pytest

from retail_intel_agent.tools import retail_tools as t
from retail_intel_agent.tools import schemas as S


@pytest.fixture
def ctx():
    """A minimal stand-in for ADK's ToolContext: tools only use `.state`."""
    return SimpleNamespace(state={})


def test_list_supported_companies_has_all_six():
    result = t.list_supported_companies()
    assert result["status"] == "success"
    keys = {c["key"] for c in result["companies"]}
    assert keys == {"walmart", "amazon", "costco", "target", "kroger", "home_depot"}


@pytest.mark.parametrize("name", ["Walmart", "wmt", "  COSTCO ", "The Home Depot", "HD"])
def test_get_company_profile_resolves_aliases(name):
    result = t.get_company_profile(name)
    assert result["status"] == "success"
    profile = result["profile"]
    assert profile["revenue_usd_billions"] > 0
    assert 0 < profile["operating_margin_pct"] < 100


def test_get_company_profile_unknown_company_returns_error():
    result = t.get_company_profile("Acme Corp")
    assert result["status"] == "error"
    assert "Unknown company" in result["error_message"]


def test_compare_companies_ranks_descending_and_stores_state(ctx):
    result = t.compare_companies(["Target", "Walmart", "Costco"], "revenue_usd_billions", ctx)
    assert result["status"] == "success"
    values = [r["value"] for r in result["ranking"]]
    assert values == sorted(values, reverse=True)
    assert result["ranking"][0]["key"] == "walmart"
    assert set(result["walmart_vs_others"]) == {
        "Costco Wholesale Corporation",
        "Target Corporation",
    }
    assert ctx.state[t.STATE_LAST_COMPARED] == ["walmart", "costco", "target"]


def test_compare_companies_rejects_bad_metric(ctx):
    result = t.compare_companies(["Walmart", "Amazon"], "vibes", ctx)
    assert result["status"] == "error"


def test_compare_companies_needs_two(ctx):
    assert t.compare_companies(["Walmart"], "store_count", ctx)["status"] == "error"


def test_compare_companies_unknown_company(ctx):
    result = t.compare_companies(["Walmart", "Nope"], "store_count", ctx)
    assert result["status"] == "error"
    assert "Nope" in result["error_message"]


def test_build_comparison_table_includes_all_metrics():
    result = t.build_comparison_table(["Walmart", "Amazon", "Kroger"])
    assert result["status"] == "success"
    assert len(result["table"]) == 3
    for row in result["table"]:
        for metric in t.NUMERIC_METRICS + t.DERIVED_METRICS:
            assert metric in row
    assert "approximate" in result["data_note"].lower()


def test_build_comparison_table_empty():
    assert t.build_comparison_table([])["status"] == "error"


def test_remember_focus_area_dedupes(ctx):
    t.remember_focus_area("Retail Media", ctx)
    result = t.remember_focus_area("retail media", ctx)
    assert result["focus_areas"] == ["retail media"]
    t.remember_focus_area("grocery", ctx)
    assert ctx.state[t.STATE_FOCUS_AREAS] == ["retail media", "grocery"]


def test_remember_focus_area_validation(ctx):
    assert t.remember_focus_area("", ctx)["status"] == "error"
    assert t.remember_focus_area("x" * 61, ctx)["status"] == "error"


def test_save_insight_and_profile(ctx):
    t.save_insight("Costco renewal rates are a loyalty benchmark.", ctx)
    t.remember_focus_area("membership", ctx)
    profile = t.get_user_profile(ctx)
    assert profile["focus_areas"] == ["membership"]
    assert profile["saved_insights"] == ["Costco renewal rates are a loyalty benchmark."]


def test_save_insight_caps_history(ctx):
    for i in range(30):
        t.save_insight(f"note {i}", ctx)
    assert len(ctx.state[t.STATE_SAVED_INSIGHTS]) == 25
    assert ctx.state[t.STATE_SAVED_INSIGHTS][-1] == "note 29"


# --------------------------------------------------------------------------
# Explicit JSON schemas
# --------------------------------------------------------------------------
def _declaration(fn):
    from google.adk.tools import FunctionTool

    return FunctionTool(fn)._get_declaration()


def _params_schema(fn) -> dict:
    decl = _declaration(fn)
    if decl.parameters_json_schema is not None:
        return decl.parameters_json_schema
    return decl.parameters.model_dump(exclude_none=True)


def test_metric_is_an_enum_in_the_function_declaration():
    schema = _params_schema(t.compare_companies)
    props = schema["properties"]
    metric = props["metric"]
    enum = metric.get("enum") or metric.get("enum_")
    assert set(enum) == set(t.NUMERIC_METRICS + t.DERIVED_METRICS)
    assert "tool_context" not in props


@pytest.mark.parametrize(
    "call,model",
    [
        (lambda c: t.list_supported_companies(), S.ListCompaniesOutput),
        (lambda c: t.get_company_profile("Target"), S.CompanyProfileOutput),
        (lambda c: t.compare_companies(["WMT", "COST"], "net_margin_pct", c), S.CompareCompaniesOutput),
        (lambda c: t.build_comparison_table(["Walmart"]), S.ComparisonTableOutput),
        (lambda c: t.remember_focus_area("pricing", c), S.FocusAreasOutput),
        (lambda c: t.save_insight("Target's Roundel is growing.", c), S.SaveInsightOutput),
        (lambda c: t.get_user_profile(c), S.UserProfileOutput),
        (lambda c: t.clear_user_memory(c), S.ClearMemoryOutput),
        (lambda c: t.get_company_profile("Nope"), S.ToolError),
        (lambda c: t.compare_companies(["Walmart"], "store_count", c), S.ToolError),
    ],
)
def test_tool_outputs_match_their_pydantic_schema(ctx, call, model):
    model.model_validate(call(ctx))


@pytest.mark.parametrize(
    "result",
    [
        lambda c: t.get_company_profile("Acme"),
        lambda c: t.compare_companies(["Walmart", "Amazon"], "vibes", c),
        lambda c: t.compare_companies(["Walmart", "Nope"], "store_count", c),
        lambda c: t.build_comparison_table([]),
        lambda c: t.remember_focus_area("", c),
        lambda c: t.save_insight("", c),
    ],
)
def test_errors_include_recovery_instructions(ctx, result):
    out = result(ctx)
    assert out["status"] == "error"
    assert out["error_message"]
    assert len(out["recovery"]) > 20


def test_clear_user_memory_wipes_user_state(ctx):
    t.remember_focus_area("grocery", ctx)
    t.save_insight("note", ctx)
    out = t.clear_user_memory(ctx)
    assert out["status"] == "success"
    assert ctx.state[t.STATE_FOCUS_AREAS] == []
    assert ctx.state[t.STATE_SAVED_INSIGHTS] == []
