"""Function tools for the Retail Competitive Intelligence Agent.

Every tool:
  * has a clear docstring (the LLM reads it to decide when to call the tool),
  * validates its inputs and never raises -- it returns a dict with a
    ``status`` of ``"success"`` or ``"error"`` so the agent can recover,
  * returns structured, JSON-serializable data.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "companies.json"

# Friendly aliases so users can type company names naturally.
_ALIASES = {
    "walmart": "walmart",
    "wmt": "walmart",
    "sam's club": "walmart",
    "amazon": "amazon",
    "amzn": "amazon",
    "whole foods": "amazon",
    "costco": "costco",
    "cost": "costco",
    "target": "target",
    "tgt": "target",
    "kroger": "kroger",
    "kr": "kroger",
    "home depot": "home_depot",
    "the home depot": "home_depot",
    "home_depot": "home_depot",
    "hd": "home_depot",
}

NUMERIC_METRICS = (
    "revenue_usd_billions",
    "operating_income_usd_billions",
    "net_income_usd_billions",
    "employees_thousands",
    "store_count",
)

# Derived metrics computed on the fly.
DERIVED_METRICS = (
    "operating_margin_pct",
    "net_margin_pct",
    "revenue_per_employee_usd_thousands",
    "revenue_per_store_usd_millions",
)

FOCUS_AREAS = (
    "e-commerce",
    "grocery",
    "membership",
    "retail media",
    "supply chain",
    "pricing",
    "private label",
    "ai and technology",
    "financial performance",
    "customer experience",
)

STATE_FOCUS_AREAS = "user:focus_areas"
STATE_SAVED_INSIGHTS = "user:saved_insights"
STATE_LAST_COMPARED = "last_compared_companies"


@lru_cache(maxsize=1)
def _load_dataset() -> dict[str, Any]:
    with _DATA_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _resolve(company: str) -> str | None:
    key = (company or "").strip().lower()
    return _ALIASES.get(key)


def _with_derived(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    rev = record["revenue_usd_billions"]
    out["operating_margin_pct"] = round(
        record["operating_income_usd_billions"] / rev * 100, 1
    )
    out["net_margin_pct"] = round(record["net_income_usd_billions"] / rev * 100, 1)
    out["revenue_per_employee_usd_thousands"] = round(
        rev * 1_000_000 / (record["employees_thousands"] * 1000), 0
    )
    out["revenue_per_store_usd_millions"] = round(
        rev * 1000 / record["store_count"], 1
    )
    return out


def list_supported_companies() -> dict[str, Any]:
    """Lists the retailers available in the built-in public-data dataset.

    Use this when the user asks which companies can be analyzed, or before
    comparing companies if you are unsure a name is supported.

    Returns:
        A dict with ``status`` and ``companies`` (key, name, ticker).
    """
    data = _load_dataset()["companies"]
    companies = [
        {"key": k, "name": v["name"], "ticker": v["ticker"]} for k, v in data.items()
    ]
    return {"status": "success", "companies": companies}


def get_company_profile(company: str) -> dict[str, Any]:
    """Returns the public profile and key financials for one retailer.

    Args:
        company: Company name or ticker, e.g. "Walmart", "COST", "Home Depot".

    Returns:
        A dict with ``status`` and ``profile`` (financials, derived ratios,
        business model, membership program, retail media network, strengths
        and challenges), or an ``error_message`` if the company is unknown.
    """
    key = _resolve(company)
    if key is None:
        return {
            "status": "error",
            "error_message": (
                f"Unknown company '{company}'. Supported: "
                + ", ".join(sorted(set(_ALIASES.values())))
            ),
        }
    record = _load_dataset()["companies"][key]
    return {"status": "success", "profile": _with_derived(record)}


def compare_companies(
    companies: list[str], metric: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Ranks several retailers on a single financial metric.

    Args:
        companies: Two or more company names or tickers, e.g.
            ["Walmart", "Amazon", "Costco"].
        metric: One of: revenue_usd_billions, operating_income_usd_billions,
            net_income_usd_billions, employees_thousands, store_count,
            operating_margin_pct, net_margin_pct,
            revenue_per_employee_usd_thousands, revenue_per_store_usd_millions.

    Returns:
        A dict with ``status``, the ``metric``, a ``ranking`` (highest first)
        and, when Walmart is included, ``walmart_vs_others`` showing how
        Walmart compares to each competitor as a ratio.
    """
    allowed = NUMERIC_METRICS + DERIVED_METRICS
    if metric not in allowed:
        return {
            "status": "error",
            "error_message": f"Unknown metric '{metric}'. Use one of: {', '.join(allowed)}",
        }
    if not companies or len(companies) < 2:
        return {
            "status": "error",
            "error_message": "Provide at least two companies to compare.",
        }

    rows, unknown = [], []
    data = _load_dataset()["companies"]
    for name in companies:
        key = _resolve(name)
        if key is None:
            unknown.append(name)
            continue
        rec = _with_derived(data[key])
        rows.append(
            {
                "key": key,
                "company": rec["name"],
                "fiscal_year": rec["fiscal_year"],
                "value": rec[metric],
            }
        )
    if unknown:
        return {
            "status": "error",
            "error_message": f"Unknown companies: {', '.join(unknown)}",
        }

    rows.sort(key=lambda r: r["value"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    result: dict[str, Any] = {"status": "success", "metric": metric, "ranking": rows}
    walmart = next((r for r in rows if r["key"] == "walmart"), None)
    if walmart:
        result["walmart_vs_others"] = {
            r["company"]: round(walmart["value"] / r["value"], 2)
            for r in rows
            if r["key"] != "walmart" and r["value"]
        }

    # Short-term memory: remember what was compared in this session.
    tool_context.state[STATE_LAST_COMPARED] = [r["key"] for r in rows]
    return result


def build_comparison_table(companies: list[str]) -> dict[str, Any]:
    """Builds a side-by-side scorecard of all key metrics for several retailers.

    Args:
        companies: Company names or tickers to include, e.g.
            ["Walmart", "Amazon", "Costco", "Target", "Kroger", "Home Depot"].

    Returns:
        A dict with ``status`` and ``table``: a list of rows (one per company)
        containing every numeric and derived metric, plus ``data_note``
        reminding the reader that figures are approximate.
    """
    data = _load_dataset()["companies"]
    table, unknown = [], []
    for name in companies or []:
        key = _resolve(name)
        if key is None:
            unknown.append(name)
            continue
        rec = _with_derived(data[key])
        table.append(
            {"company": rec["name"], "fiscal_year": rec["fiscal_year"]}
            | {m: rec[m] for m in NUMERIC_METRICS + DERIVED_METRICS}
        )
    if unknown:
        return {
            "status": "error",
            "error_message": f"Unknown companies: {', '.join(unknown)}",
        }
    if not table:
        return {"status": "error", "error_message": "No companies provided."}
    return {
        "status": "success",
        "table": table,
        "data_note": _load_dataset()["_metadata"]["description"],
    }


def remember_focus_area(focus_area: str, tool_context: ToolContext) -> dict[str, Any]:
    """Saves a strategic focus area the user cares about, for future sessions.

    Call this whenever the user states a priority, e.g. "I care most about
    retail media" or "focus on grocery".

    Args:
        focus_area: A short topic such as "retail media", "grocery",
            "membership", "e-commerce", "supply chain", or "pricing".

    Returns:
        A dict with ``status`` and the full list of saved ``focus_areas``.
    """
    area = (focus_area or "").strip().lower()
    if not area:
        return {"status": "error", "error_message": "focus_area cannot be empty."}
    if len(area) > 60:
        return {"status": "error", "error_message": "Keep focus areas under 60 characters."}
    current = list(tool_context.state.get(STATE_FOCUS_AREAS, []))
    if area not in current:
        current.append(area)
    tool_context.state[STATE_FOCUS_AREAS] = current
    return {"status": "success", "focus_areas": current}


def save_insight(insight: str, tool_context: ToolContext) -> dict[str, Any]:
    """Saves a key takeaway to the user's strategy notebook (persists across sessions).

    Use this when the user asks to "save", "remember", or "note" a finding.

    Args:
        insight: One or two sentences summarizing the takeaway.

    Returns:
        A dict with ``status`` and the number of ``saved_insights``.
    """
    text = (insight or "").strip()
    if not text:
        return {"status": "error", "error_message": "insight cannot be empty."}
    notes = list(tool_context.state.get(STATE_SAVED_INSIGHTS, []))
    notes.append(text[:500])
    tool_context.state[STATE_SAVED_INSIGHTS] = notes[-25:]  # keep the latest 25
    return {"status": "success", "saved_insights": len(notes[-25:])}


def get_user_profile(tool_context: ToolContext) -> dict[str, Any]:
    """Returns what the agent remembers about the user.

    Includes saved focus areas, saved insights, and the companies compared
    most recently in this session.

    Returns:
        A dict with ``status``, ``focus_areas``, ``saved_insights`` and
        ``last_compared_companies``.
    """
    return {
        "status": "success",
        "focus_areas": tool_context.state.get(STATE_FOCUS_AREAS, []),
        "saved_insights": tool_context.state.get(STATE_SAVED_INSIGHTS, []),
        "last_compared_companies": tool_context.state.get(STATE_LAST_COMPARED, []),
    }
