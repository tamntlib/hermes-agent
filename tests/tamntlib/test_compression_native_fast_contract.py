"""Compression consumes the native Fast signature/gate, never proxy declarations."""
from tests.agent.conftest import (
    _fast_retry_backoff,
    _fresh_structured_output_memo,
)

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent import auxiliary_client as aux
from agent.auxiliary_service_tiers import apply_compression_service_tier
from tests.tamntlib.test_auxiliary_compression_service_tiers import CaptureClient

pytest_plugins = ("tests.tamntlib.test_auxiliary_compression_service_tiers",)


def invoke(async_mode, **kwargs):
    import asyncio
    return asyncio.run(aux.async_call_llm(**kwargs)) if async_mode else aux.call_llm(**kwargs)


def failing_client(client, async_mode):
    original = client.create
    def fail(**kwargs):
        original(**kwargs)
        error = RuntimeError("401 authentication failed")
        error.status_code = 401
        raise error
    client.create = fail
    client.chat.completions.create = client.acreate if async_mode else fail
    return client


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("selection", ["openai", "main", "auto"])
def test_native_call_selection(config, monkeypatch, async_mode, selection):
    client = CaptureClient(async_mode=async_mode, effective="openai", url="https://api.openai.com/v1")
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, "gpt-5.5"))
    config["auxiliary"]["compression"]["provider"] = selection
    invoke(async_mode, task="compression", provider=None if selection == "auto" else selection,
        main_runtime={"provider": "openai", "model": "gpt-5.5", "base_url": client.base_url,
                      "api_mode": "chat_completions"}, messages=[{"role": "user", "content": "summarize"}])
    assert client.requests[0]["extra_body"]["service_tier"] == "priority"


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("destination", ["same", "model", "endpoint"])
def test_native_credential_retry_reprojects_destination(config, monkeypatch, async_mode, destination):
    first = failing_client(CaptureClient(async_mode=async_mode, effective="openai", url="https://api.openai.com/v1"), async_mode)
    retry = CaptureClient(async_mode=async_mode, effective="openai", url=(
        "https://relay.example/v1" if destination == "endpoint" else first.base_url))
    model = "unsupported-model" if destination == "model" else "gpt-5.5"
    clients = iter([(first, "gpt-5.5"), (retry, model)])
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: next(clients))
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **k: True)
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route", lambda *a, **k: "openai")
    invoke(async_mode, task="compression", provider="openai", messages=[{"role": "user", "content": "summarize"}],
        extra_body={"keep": 7})
    assert first.requests[0]["extra_body"] == {"keep": 7, "service_tier": "priority"}
    assert retry.requests[0]["model"] == model
    assert retry.requests[0]["extra_body"] == {"keep": 7, "service_tier": "priority"}


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("chain_kind", ["task", "main"])
@pytest.mark.parametrize("native", [True, False])
def test_native_primary_to_fallback_destination(config, monkeypatch, async_mode, chain_kind, native):
    first = failing_client(CaptureClient(async_mode=async_mode, effective="openai", url="https://api.openai.com/v1"), async_mode)
    provider = "anthropic" if native else "custom:paid"
    url = "https://api.anthropic.com" if native else "https://relay.example/v1"
    model = "claude-opus-4-8"
    entry = {"provider": provider, "model": model, "base_url": url, "api_mode": "anthropic_messages"}
    if chain_kind == "task":
        config["auxiliary"]["compression"]["fallback_chain"] = [entry]
    else:
        config["fallback_providers"] = [entry]
    captured = []
    monkeypatch.setattr("agent.anthropic_adapter.create_anthropic_message", lambda client, kwargs, **_: (
        captured.append(kwargs) or SimpleNamespace(content=[SimpleNamespace(type="text", text="summary")],
                                                   stop_reason="end_turn", usage=None)))
    second = aux.AnthropicAuxiliaryClient(SimpleNamespace(), model, "test-only", url)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (first, "gpt-5.5"))
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **k: False)
    monkeypatch.setattr(aux, "resolve_provider_client", lambda *a, **k: (second, model))
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda *a, **k: False)
    monkeypatch.setattr(aux, "_mark_provider_unhealthy", lambda *a, **k: None)
    invoke(async_mode, task="compression", provider=None if chain_kind == "main" else "openai",
        main_runtime={"provider": "openai", "model": "gpt-5.5", "base_url": first.base_url,
                      "api_mode": "chat_completions"},
        messages=[{"role": "user", "content": "summarize"}], extra_body={"keep": 7})
    assert first.requests[0]["extra_body"] == {"keep": 7, "service_tier": "priority"}
    assert captured[0]["model"] == model
    assert captured[0]["extra_body"] == {"keep": 7, "speed": "fast"}
    assert "fast-mode-2026-02-01" in captured[0].get("extra_headers", {}).get("anthropic-beta", "")


@pytest.mark.parametrize("provider,url,model,tier,expected", [
    ("openai", "https://api.openai.com/v1", "gpt-5.5", "priority", {"service_tier": "priority"}),
    ("openai-codex", "https://chatgpt.com/backend-api/codex", "gpt-6-astra", "ultrafast", {"service_tier": "ultrafast"}),
    ("anthropic", "https://api.anthropic.com", "claude-opus-4-8", "priority", {"speed": "fast"}),
    ("anthropic", "https://api.anthropic.com", "claude-opus-4-8", "ultrafast", {}),
    ("custom:paid", "https://relay.example/v1", "gpt-5.5", "priority", {"service_tier": "priority"}),
    ("openai", "https://relay.example/v1", "gpt-5.5", "priority", {"service_tier": "priority"}),
    ("openai", "https://api.openai.com/v1", "unsupported-model", "priority", {"service_tier": "priority"}),
])
def test_native_signature_and_destination_gate(provider, url, model, tier, expected):
    original = {"model": model, "extra_body": {"keep": 7, "speed": "fast", "service_tier": "priority"}}
    saved = deepcopy(original)
    result = apply_compression_service_tier(original, {"service_tier": tier},
        provider=provider, model=model, base_url=url,
        api_mode="anthropic_messages" if provider == "anthropic" else "chat_completions")
    assert result["extra_body"] == {"keep": 7, **expected}
    assert original == saved


@pytest.mark.parametrize("provider", ["anthropic", "custom:paid"])
@pytest.mark.parametrize("task,policy,expected", [
    ("compression", "priority", True), ("compression", "normal", False),
    ("compression", None, False), ("vision", "priority", False),
    (None, "priority", False),
])
def test_native_anthropic_beta_requires_compression_projection(monkeypatch, task, policy, expected, provider):
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda _: {"service_tier": policy})
    captured = []
    monkeypatch.setattr("agent.anthropic_adapter.create_anthropic_message",
        lambda client, kwargs, **_: captured.append(kwargs) or SimpleNamespace(
            content=[SimpleNamespace(type="text", text="summary")], stop_reason="end_turn", usage=None))
    client = aux.AnthropicAuxiliaryClient(SimpleNamespace(), "claude-opus-4-8", "test-only",
                                         "https://api.anthropic.com")
    token = aux._RELAY_AUX_CALL_CONTEXT.set({"task": task, "provider": provider})
    try:
        client.chat.completions.create(model="claude-opus-4-8", messages=[],
                                       extra_body={"speed": "fast", "keep": 7})
    finally:
        aux._RELAY_AUX_CALL_CONTEXT.reset(token)
    body = captured[0].get("extra_body", {})
    assert body["keep"] == 7
    assert ("fast-mode-2026-02-01" in captured[0].get("extra_headers", {}).get("anthropic-beta", "")) is expected
    # Raw extras remain a legacy passthrough, but cannot activate Hermes' native beta.
    assert body["speed"] == "fast"
