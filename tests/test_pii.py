"""Tests for PII redaction (logs, state and memory writes)."""

import json
import logging

import pytest

from retail_intel_agent.observability import _JsonFormatter
from retail_intel_agent.pii import contains_pii, redact, redact_obj
from retail_intel_agent.tools import retail_tools as t


@pytest.mark.parametrize(
    "text,label",
    [
        ("mail jane.doe@example.com now", "EMAIL"),
        ("call (479) 555-0123 today", "PHONE"),
        ("call +1 479-555-0123 today", "PHONE"),
        ("ssn 123-45-6789", "SSN"),
        ("card 4111 1111 1111 1111", "CREDIT_CARD"),
        ("card 4111-1111-1111-1111", "CREDIT_CARD"),
        ("key AIza" + "A" * 35, "API_KEY"),
        ("token sk-" + "a1B2" * 6, "API_KEY"),
        ("Authorization: Bearer " + "abcdefghij" * 3, "API_KEY"),
    ],
)
def test_redact_detects_pii(text, label):
    out = redact(text)
    assert f"[REDACTED_{label}]" in out
    assert contains_pii(text)


@pytest.mark.parametrize(
    "text",
    [
        "Walmart revenue was $681.0B in FY2025 with 10,750 stores.",
        "Operating margin 4.3% vs Costco 3.7%",
        "Compare Walmart vs Target on retail media",
    ],
)
def test_redact_leaves_business_text_alone(text):
    assert redact(text) == text
    assert not contains_pii(text)


def test_redact_obj_is_recursive():
    data = {"a": ["x@y.com", {"b": "123-45-6789"}], "n": 5, "t": ("555-123-4567",)}
    out = redact_obj(data)
    assert out["a"][0] == "[REDACTED_EMAIL]"
    assert out["a"][1]["b"] == "[REDACTED_SSN]"
    assert out["n"] == 5
    assert isinstance(out["t"], tuple) and "REDACTED" in out["t"][0]


def test_save_insight_redacts_before_persisting():
    ctx = type("Ctx", (), {"state": {}})()
    result = t.save_insight("Ping jane@corp.com about Costco renewals", ctx)
    assert result["status"] == "success"
    assert "jane@corp.com" not in ctx.state[t.STATE_SAVED_INSIGHTS][0]
    assert "[REDACTED_EMAIL]" in result["stored_text"]


def test_remember_focus_area_redacts():
    ctx = type("Ctx", (), {"state": {}})()
    t.remember_focus_area("call 479-555-0123", ctx)
    assert ctx.state[t.STATE_FOCUS_AREAS] == ["call [REDACTED_PHONE]"]


def test_json_log_formatter_redacts_message_and_fields():
    record = logging.LogRecord("retail_intel_agent", logging.INFO, __file__, 1, "user bob@x.io", None, None)
    record.fields = {"args": {"note": "ssn 123-45-6789"}, "correlation_id": "abc"}
    line = json.loads(_JsonFormatter().format(record))
    assert "bob@x.io" not in line["message"]
    assert "123-45-6789" not in json.dumps(line)
    assert line["correlation_id"] == "abc"
