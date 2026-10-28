"""PII redaction for logs, session state and long-term memory.

The agent is designed for public data only, but users can still paste
personal data by accident (an email signature, a phone number, an API key).
Everything that is *persisted or emitted* passes through :func:`redact` /
:func:`redact_obj` first:

* every structured log line (see ``observability._JsonFormatter``),
* focus areas and insights written to user-scoped state
  (``remember_focus_area`` / ``save_insight``).

This is a fast, dependency-free regex layer. For production-grade detection
(names, addresses, 100+ infoTypes) swap in **Google Cloud Sensitive Data
Protection (Cloud DLP)** ``deidentify_content`` behind the same function
signature -- callers do not need to change.
"""

from __future__ import annotations

import re
from typing import Any

# Order matters: more specific patterns run first so that, e.g., an SSN is not
# partially consumed by the phone-number pattern.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Google API keys, OpenAI-style keys, GitHub tokens, AWS access keys,
    # generic "Bearer <token>" headers.
    (
        "API_KEY",
        re.compile(
            r"\b(?:AIza[0-9A-Za-z_\-]{35}|sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{36,}"
            r"|AKIA[0-9A-Z]{16})\b|(?i:bearer\s+[A-Za-z0-9._\-]{20,})"
        ),
    ),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # 13-19 digit card numbers, optionally separated by spaces or dashes.
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    (
        "PHONE",
        re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b"),
    ),
)


def redact(text: str) -> str:
    """Replaces PII in ``text`` with ``[REDACTED_<TYPE>]`` placeholders.

    Args:
        text: Any string (log message, user note, tool argument).

    Returns:
        The same string with emails, phone numbers, SSNs, credit-card numbers
        and API-key-like secrets replaced. Non-strings are returned unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[REDACTED_{label}]", text)
    return text


def redact_obj(value: Any) -> Any:
    """Recursively redacts PII in strings inside dicts, lists and tuples."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_obj(v) for v in value)
    return value


def contains_pii(text: str) -> bool:
    """Returns True if :func:`redact` would change ``text``."""
    return isinstance(text, str) and redact(text) != text
