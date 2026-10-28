"""Unit tests for the function tools (no API key or network needed)."""

from types import SimpleNamespace

import pytest

from retail_intel_agent.tools import retail_tools as t


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
