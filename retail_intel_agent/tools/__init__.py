"""Tools package."""

from .retail_tools import (
    build_comparison_table,
    compare_companies,
    get_company_profile,
    get_user_profile,
    list_supported_companies,
    remember_focus_area,
    save_insight,
)

__all__ = [
    "build_comparison_table",
    "compare_companies",
    "get_company_profile",
    "get_user_profile",
    "list_supported_companies",
    "remember_focus_area",
    "save_insight",
]
