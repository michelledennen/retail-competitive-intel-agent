"""Tests for configuration: model routing and secure secret loading."""

from __future__ import annotations

import sys
import types

from retail_intel_agent import agent as agent_module
from retail_intel_agent import config


def _name(agent):
    return agent.model if isinstance(agent.model, str) else agent.model.model


def test_model_routing_fast_for_specialists_pro_for_reasoning():
    pipeline = agent_module.competitive_report_pipeline
    fanout, writer, reviewer = pipeline.sub_agents
    assert _name(agent_module.root_agent) == config.PRO_MODEL
    assert _name(writer) == config.PRO_MODEL
    assert _name(reviewer) == config.PRO_MODEL
    assert _name(agent_module.financial_analyst) == config.FAST_MODEL
    assert _name(agent_module.market_researcher) == config.FAST_MODEL
    assert all(_name(a) == config.FAST_MODEL for a in fanout.sub_agents)


def test_models_retry_transient_errors_but_not_quota():
    retry = agent_module.root_agent.model.retry_options
    assert retry.attempts >= 3
    assert 503 in retry.http_status_codes
    assert 429 not in retry.http_status_codes


def test_api_key_prefers_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "from-env")
    monkeypatch.setenv("GOOGLE_API_KEY_SECRET", "projects/p/secrets/s/versions/latest")
    assert config.load_google_api_key() == "from-env"


def test_api_key_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY_SECRET", raising=False)
    assert config.load_google_api_key() is None


def test_api_key_loaded_from_secret_manager(monkeypatch):
    """Secret Manager is used when GOOGLE_API_KEY_SECRET is set (client is faked)."""
    calls = {}

    class FakeClient:
        def access_secret_version(self, name):
            calls["name"] = name
            payload = types.SimpleNamespace(data=b"secret-value\n")
            return types.SimpleNamespace(payload=payload)

    fake_module = types.ModuleType("google.cloud.secretmanager")
    fake_module.SecretManagerServiceClient = FakeClient
    import google.cloud as google_cloud

    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_module)
    monkeypatch.setattr(google_cloud, "secretmanager", fake_module, raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY_SECRET", "projects/p/secrets/gemini/versions/latest")

    assert config.load_google_api_key() == "secret-value"
    assert calls["name"] == "projects/p/secrets/gemini/versions/latest"
    import os

    assert os.environ["GOOGLE_API_KEY"] == "secret-value"
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def test_api_key_secret_errors_do_not_crash(monkeypatch):
    class BrokenClient:
        def access_secret_version(self, name):
            raise PermissionError("denied")

    fake_module = types.ModuleType("google.cloud.secretmanager")
    fake_module.SecretManagerServiceClient = BrokenClient
    import google.cloud as google_cloud

    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_module)
    monkeypatch.setattr(google_cloud, "secretmanager", fake_module, raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY_SECRET", "projects/p/secrets/gemini/versions/latest")
    assert config.load_google_api_key() is None
