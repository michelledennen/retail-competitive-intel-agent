"""Explicit JSON schemas (Pydantic models + Literal enums) for every tool.

Why this matters:

* **Inputs** -- ``Metric`` and ``CompanyKey`` are ``typing.Literal`` enums, so
  the ``FunctionDeclaration`` ADK sends to Gemini contains an ``enum`` list:
  the model *cannot* invent a metric name. Free-text inputs are validated
  against Pydantic input models (length limits, minimum item counts).
* **Outputs** -- every tool builds a Pydantic output model and returns
  ``model.model_dump()``. If a tool ever produced a malformed payload, it
  would fail validation in our unit tests instead of confusing the LLM.
* **Errors** -- all failures use one :class:`ToolError` shape with an explicit
  ``recovery`` instruction telling the agent exactly what to do next.

``python -c "from retail_intel_agent.tools.schemas import CompareCompaniesOutput as M; print(M.model_json_schema())"``
prints the JSON Schema of any model.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CompanyKey = Literal["walmart", "amazon", "costco", "target", "kroger", "home_depot"]

NumericMetric = Literal[
    "revenue_usd_billions",
    "operating_income_usd_billions",
    "net_income_usd_billions",
    "employees_thousands",
    "store_count",
]
DerivedMetric = Literal[
    "operating_margin_pct",
    "net_margin_pct",
    "revenue_per_employee_usd_thousands",
    "revenue_per_store_usd_millions",
]
Metric = Literal[
    "revenue_usd_billions",
    "operating_income_usd_billions",
    "net_income_usd_billions",
    "employees_thousands",
    "store_count",
    "operating_margin_pct",
    "net_margin_pct",
    "revenue_per_employee_usd_thousands",
    "revenue_per_store_usd_millions",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
class CompanyNameInput(_Strict):
    company: str = Field(min_length=1, max_length=60, description="Company name or ticker.")


class CompareCompaniesInput(_Strict):
    companies: list[str] = Field(
        min_length=2, max_length=6, description="Two to six company names or tickers."
    )
    metric: Metric = Field(description="Metric to rank on (highest first).")


class ComparisonTableInput(_Strict):
    companies: list[str] = Field(
        min_length=1, max_length=6, description="One to six company names or tickers."
    )


class FocusAreaInput(_Strict):
    focus_area: str = Field(min_length=1, max_length=60, description="Short topic, e.g. 'grocery'.")


class InsightInput(_Strict):
    insight: str = Field(min_length=1, max_length=500, description="One or two sentences.")


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------
class ToolError(_Strict):
    status: Literal["error"] = "error"
    error_message: str
    recovery: str = Field(description="Explicit next step the agent should take.")
    valid_options: Optional[list[str]] = None


class CompanySummary(_Strict):
    key: CompanyKey
    name: str
    ticker: str


class ListCompaniesOutput(_Strict):
    status: Literal["success"] = "success"
    companies: list[CompanySummary]
    metrics: list[str]


class CompanyProfile(_Strict):
    name: str
    ticker: str
    fiscal_year: str
    headquarters: str
    revenue_usd_billions: float
    operating_income_usd_billions: float
    net_income_usd_billions: float
    employees_thousands: int
    store_count: int
    operating_margin_pct: float
    net_margin_pct: float
    revenue_per_employee_usd_thousands: float
    revenue_per_store_usd_millions: float
    business_model: str
    membership_program: str
    retail_media_network: str
    ecommerce_highlights: str
    key_strengths: list[str]
    key_challenges: list[str]


class CompanyProfileOutput(_Strict):
    status: Literal["success"] = "success"
    profile: CompanyProfile


class RankingRow(_Strict):
    rank: int = Field(ge=1)
    key: CompanyKey
    company: str
    fiscal_year: str
    value: float


class CompareCompaniesOutput(_Strict):
    status: Literal["success"] = "success"
    metric: Metric
    ranking: list[RankingRow]
    walmart_vs_others: Optional[dict[str, float]] = Field(
        default=None, description="Walmart value divided by each competitor's value."
    )


class ScorecardRow(_Strict):
    company: str
    fiscal_year: str
    revenue_usd_billions: float
    operating_income_usd_billions: float
    net_income_usd_billions: float
    employees_thousands: int
    store_count: int
    operating_margin_pct: float
    net_margin_pct: float
    revenue_per_employee_usd_thousands: float
    revenue_per_store_usd_millions: float


class ComparisonTableOutput(_Strict):
    status: Literal["success"] = "success"
    table: list[ScorecardRow]
    data_note: str


class FocusAreasOutput(_Strict):
    status: Literal["success"] = "success"
    focus_areas: list[str]


class SaveInsightOutput(_Strict):
    status: Literal["success"] = "success"
    saved_insights: int
    stored_text: str = Field(description="The insight as stored (PII redacted).")


class UserProfileOutput(_Strict):
    status: Literal["success"] = "success"
    focus_areas: list[str]
    saved_insights: list[str]
    last_compared_companies: list[str]


class ClearMemoryOutput(_Strict):
    status: Literal["success"] = "success"
    cleared: list[str]
    message: str


class BriefChecks(BaseModel):
    numbers_grounded: bool = Field(
        description="Every number in the brief appears in the financial findings."
    )
    all_sections_present: bool = Field(
        description="Headline, Scorecard, Competitor-by-competitor, SWOT, Talking points."
    )
    disclaimer_present: bool = Field(description="States figures are approximate and public.")


class BriefReview(BaseModel):
    """Structured output of the ``brief_quality_reviewer`` self-evaluation step."""

    verdict: Literal["pass", "revised"] = Field(
        description="'pass' if the draft met every check, 'revised' if you fixed it."
    )
    checks: BriefChecks
    issues_found: list[str] = Field(
        default_factory=list, description="Problems found in the draft (empty if none)."
    )
    final_brief: str = Field(description="The approved or corrected brief, in markdown.")
