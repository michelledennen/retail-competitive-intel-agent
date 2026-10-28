"""Function tools for the Retail Competitive Intelligence Agent.

Every tool:
  * has a clear docstring (the LLM reads it to decide when to call the tool),
  * declares an explicit JSON schema: ``Literal`` enums for constrained inputs
    (see :mod:`.schemas`) and a Pydantic output model that is validated before
    the tool returns ``model_dump()``,
  * never raises -- on bad input it returns a :class:`~.schemas.ToolError`
    with ``status="error"``, an ``error_message`` and an explicit
    ``recovery`` instruction so the agent knows how to fix the call,
  * redacts PII before anything is written to persistent user state.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext
from pydantic import BaseModel, ValidationError

from ..pii import contains_pii, redact
from .schemas import (
    ClearMemoryOutput,
    CompanyNameInput,
    CompanyProfile,
    CompanyProfileOutput,
    CompanySummary,
    CompareCompaniesInput,
    CompareCompaniesOutput,
    ComparisonTableInput,
    ComparisonTableOutput,
    FocusAreaInput,
    FocusAreasOutput,
    InsightInput,
    ListCompaniesOutput,
    Metric,
    RankingRow,
    SaveInsightOutput,
    ScorecardRow,
    ToolError,
    UserProfileOutput,
)

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

MAX_SAVED_INSIGHTS = 25

_SUPPORTED_HINT = "Walmart, Amazon, Costco, Target, Kroger, Home Depot (or tickers WMT, AMZN, COST, TGT, KR, HD)"


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
    out["operating_margin_pct"] = round(record["operating_income_usd_billions"] / rev * 100, 1)
    out["net_margin_pct"] = round(record["net_income_usd_billions"] / rev * 100, 1)
    out["revenue_per_employee_usd_thousands"] = round(
        rev * 1_000_000 / (record["employees_thousands"] * 1000), 0
    )
    out["revenue_per_store_usd_millions"] = round(rev * 1000 / record["store_count"], 1)
    return out


def _error(message: str, recovery: str, valid_options: list[str] | None = None) -> dict[str, Any]:
    return ToolError(
        error_message=message, recovery=recovery, valid_options=valid_options
    ).model_dump(exclude_none=True)


def _validation_error(exc: ValidationError, recovery: str) -> dict[str, Any]:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
    )
    return _error(f"Invalid arguments: {problems}", recovery)


def _ok(model: BaseModel) -> dict[str, Any]:
    """Validates the output model and returns it as a JSON-serializable dict."""
    return type(model).model_validate(model.model_dump()).model_dump(exclude_none=True)


def _unknown_companies_error(unknown: list[str]) -> dict[str, Any]:
    return _error(
        f"Unknown companies: {', '.join(unknown)}.",
        recovery=(
            "Replace the unknown names with supported ones and call the tool again. "
            f"Supported: {_SUPPORTED_HINT}. If the user asked about a retailer that "
            "is not supported, tell them it is outside the dataset and offer to use "
            "market_researcher (web search) instead."
        ),
        valid_options=sorted(set(_ALIASES.values())),
    )


# --------------------------------------------------------------------------
# Financial data tools
# --------------------------------------------------------------------------
def list_supported_companies() -> dict[str, Any]:
    """Lists the retailers and metrics available in the built-in public-data dataset.

    Use this when the user asks which companies can be analyzed, or before
    comparing companies if you are unsure a name is supported.

    Returns:
        A dict with ``status``, ``companies`` (key, name, ticker) and the list
        of ``metrics`` that ``compare_companies`` accepts.
    """
    data = _load_dataset()["companies"]
    return _ok(
        ListCompaniesOutput(
            companies=[CompanySummary(key=k, name=v["name"], ticker=v["ticker"]) for k, v in data.items()],
            metrics=list(NUMERIC_METRICS + DERIVED_METRICS),
        )
    )


def get_company_profile(company: str) -> dict[str, Any]:
    """Returns the public profile and key financials for one retailer.

    Args:
        company: Company name or ticker, e.g. "Walmart", "COST", "Home Depot".

    Returns:
        A dict with ``status`` and ``profile`` (financials, derived ratios,
        business model, membership program, retail media network, strengths
        and challenges), or an error with a ``recovery`` instruction if the
        company is unknown.
    """
    try:
        CompanyNameInput(company=company or "")
    except ValidationError as exc:
        return _validation_error(exc, f"Call again with one company name, e.g. 'Walmart'. Supported: {_SUPPORTED_HINT}.")
    key = _resolve(company)
    if key is None:
        return _error(
            f"Unknown company '{company}'.",
            recovery=(
                f"Retry with one of: {_SUPPORTED_HINT}. Call list_supported_companies "
                "if unsure; for other retailers use web search instead."
            ),
            valid_options=sorted(set(_ALIASES.values())),
        )
    record = _with_derived(_load_dataset()["companies"][key])
    return _ok(CompanyProfileOutput(profile=CompanyProfile(**record)))


def compare_companies(
    companies: list[str], metric: Metric, tool_context: ToolContext
) -> dict[str, Any]:
    """Ranks several retailers on a single financial metric.

    Args:
        companies: Two to six company names or tickers, e.g.
            ["Walmart", "Amazon", "Costco"].
        metric: The metric to rank on. One of: revenue_usd_billions,
            operating_income_usd_billions, net_income_usd_billions,
            employees_thousands, store_count, operating_margin_pct,
            net_margin_pct, revenue_per_employee_usd_thousands,
            revenue_per_store_usd_millions.

    Returns:
        A dict with ``status``, the ``metric``, a ``ranking`` (highest first)
        and, when Walmart is included, ``walmart_vs_others`` showing how
        Walmart compares to each competitor as a ratio.
    """
    try:
        args = CompareCompaniesInput(companies=list(companies or []), metric=metric)
    except ValidationError as exc:
        return _validation_error(
            exc,
            recovery=(
                "Call compare_companies again with 2-6 companies and exactly one metric "
                f"from: {', '.join(NUMERIC_METRICS + DERIVED_METRICS)}. To show many "
                "metrics at once use build_comparison_table instead."
            ),
        )

    rows, unknown, seen = [], [], set()
    data = _load_dataset()["companies"]
    for name in args.companies:
        key = _resolve(name)
        if key is None:
            unknown.append(name)
            continue
        if key in seen:
            continue
        seen.add(key)
        rec = _with_derived(data[key])
        rows.append({"key": key, "company": rec["name"], "fiscal_year": rec["fiscal_year"], "value": rec[args.metric]})
    if unknown:
        return _unknown_companies_error(unknown)

    rows.sort(key=lambda r: r["value"], reverse=True)
    ranking = [RankingRow(rank=i, **r) for i, r in enumerate(rows, start=1)]
    walmart = next((r for r in ranking if r.key == "walmart"), None)
    vs_others = None
    if walmart:
        vs_others = {
            r.company: round(walmart.value / r.value, 2) for r in ranking if r.key != "walmart" and r.value
        }

    # Short-term (session-scoped) memory: remember what was compared.
    tool_context.state[STATE_LAST_COMPARED] = [r.key for r in ranking]
    return _ok(CompareCompaniesOutput(metric=args.metric, ranking=ranking, walmart_vs_others=vs_others))


def build_comparison_table(companies: list[str]) -> dict[str, Any]:
    """Builds a side-by-side scorecard of all key metrics for several retailers.

    Args:
        companies: One to six company names or tickers to include, e.g.
            ["Walmart", "Amazon", "Costco", "Target", "Kroger", "Home Depot"].

    Returns:
        A dict with ``status`` and ``table``: a list of rows (one per company)
        containing every numeric and derived metric, plus ``data_note``
        reminding the reader that figures are approximate.
    """
    try:
        args = ComparisonTableInput(companies=list(companies or []))
    except ValidationError as exc:
        return _validation_error(
            exc,
            recovery=(
                "Call build_comparison_table again with a list of 1-6 company names, "
                "for example [\"Walmart\", \"Costco\"]. Always include Walmart."
            ),
        )
    data = _load_dataset()["companies"]
    table, unknown = [], []
    for name in args.companies:
        key = _resolve(name)
        if key is None:
            unknown.append(name)
            continue
        rec = _with_derived(data[key])
        table.append(
            ScorecardRow(
                company=rec["name"],
                fiscal_year=rec["fiscal_year"],
                **{m: rec[m] for m in NUMERIC_METRICS + DERIVED_METRICS},
            )
        )
    if unknown:
        return _unknown_companies_error(unknown)
    return _ok(ComparisonTableOutput(table=table, data_note=_load_dataset()["_metadata"]["description"]))


# --------------------------------------------------------------------------
# Memory tools (user-scoped state persists across sessions with a
# persistent session service such as DatabaseSessionService)
# --------------------------------------------------------------------------
def remember_focus_area(focus_area: str, tool_context: ToolContext) -> dict[str, Any]:
    """Saves a strategic focus area the user cares about, for future sessions.

    Call this whenever the user states a priority, e.g. "I care most about
    retail media" or "focus on grocery".

    Args:
        focus_area: A short topic (max 60 characters) such as "retail media",
            "grocery", "membership", "e-commerce", "supply chain", or "pricing".

    Returns:
        A dict with ``status`` and the full list of saved ``focus_areas``.
    """
    try:
        args = FocusAreaInput(focus_area=(focus_area or "").strip())
    except ValidationError as exc:
        return _validation_error(
            exc,
            recovery=(
                "Ask the user for a short topic (under 60 characters) or pick the closest "
                f"of: {', '.join(FOCUS_AREAS)}; then call remember_focus_area again."
            ),
        )
    area = redact(args.focus_area.lower())
    current = list(tool_context.state.get(STATE_FOCUS_AREAS, []))
    if area not in current:
        current.append(area)
    tool_context.state[STATE_FOCUS_AREAS] = current
    return _ok(FocusAreasOutput(focus_areas=current))


def save_insight(insight: str, tool_context: ToolContext) -> dict[str, Any]:
    """Saves a key takeaway to the user's strategy notebook (persists across sessions).

    Use this when the user asks to "save", "remember", or "note" a finding.
    This is a persistent write, so ADK asks the user to confirm before it runs.

    Args:
        insight: One or two sentences (max 500 characters) summarizing the takeaway.

    Returns:
        A dict with ``status``, the number of ``saved_insights`` and the
        ``stored_text`` (with any personal data redacted).
    """
    try:
        args = InsightInput(insight=(insight or "").strip())
    except ValidationError as exc:
        return _validation_error(
            exc,
            recovery=(
                "Summarize the takeaway in one or two sentences (under 500 characters) "
                "and call save_insight again."
            ),
        )
    text = redact(args.insight)
    if contains_pii(args.insight):
        logger.info("save_insight: PII redacted before persisting")
    notes = list(tool_context.state.get(STATE_SAVED_INSIGHTS, []))
    notes.append(text)
    notes = notes[-MAX_SAVED_INSIGHTS:]  # keep the latest N
    tool_context.state[STATE_SAVED_INSIGHTS] = notes
    return _ok(SaveInsightOutput(saved_insights=len(notes), stored_text=text))


def get_user_profile(tool_context: ToolContext) -> dict[str, Any]:
    """Returns what the agent remembers about the user.

    Includes saved focus areas, saved insights, and the companies compared
    most recently in this session.

    Returns:
        A dict with ``status``, ``focus_areas``, ``saved_insights`` and
        ``last_compared_companies``.
    """
    return _ok(
        UserProfileOutput(
            focus_areas=list(tool_context.state.get(STATE_FOCUS_AREAS, []) or []),
            saved_insights=list(tool_context.state.get(STATE_SAVED_INSIGHTS, []) or []),
            last_compared_companies=list(tool_context.state.get(STATE_LAST_COMPARED, []) or []),
        )
    )


def clear_user_memory(tool_context: ToolContext) -> dict[str, Any]:
    """Permanently deletes the user's saved focus areas and saved insights.

    High-stakes and irreversible: ADK pauses and asks the user to approve
    before this runs. Only call it when the user explicitly asks to forget,
    reset, or wipe what the agent remembers about them.

    Returns:
        A dict with ``status``, the list of ``cleared`` memory keys and a
        confirmation ``message``.
    """
    tool_context.state[STATE_FOCUS_AREAS] = []
    tool_context.state[STATE_SAVED_INSIGHTS] = []
    return _ok(
        ClearMemoryOutput(
            cleared=[STATE_FOCUS_AREAS, STATE_SAVED_INSIGHTS],
            message="Saved focus areas and insights were deleted.",
        )
    )
