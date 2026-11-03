"""Compression paid tiers are opt-in and authorized by the exact destination."""
from __future__ import annotations

from copy import deepcopy
import asyncio
from types import SimpleNamespace

import pytest

from agent import auxiliary_client as aux

URL = "https://relay.example/v1"
MODEL = "gpt-5.5"


def route(*, tiers=("priority", "ultrafast"), mode="chat_completions", **extra):
    entry = {"base_url": URL, "api_mode": mode, "models": [MODEL], **extra}
    if tiers is not None:
        entry["service_tiers"] = list(tiers)
    return entry


@pytest.fixture
def config(monkeypatch):
    cfg = {"auxiliary": {"compression": {"service_tier": "priority"}},
           "providers": {"paid": route()}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: deepcopy(cfg))
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda: deepcopy(cfg))
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: deepcopy(cfg["auxiliary"].get(task, {})))
    monkeypatch.setattr(aux, "_with_custom_endpoint_extra_body", lambda eb, *args: deepcopy(eb or {}))
    return cfg


def build(**kwargs):
    return aux._build_call_kwargs("custom:paid", MODEL, [{"role": "user", "content": "summarize"}],
                                  base_url=URL, task="compression", **kwargs)


def test_declared_priority_reaches_request_builder(config):
    assert build()["extra_body"]["service_tier"] == "priority"


@pytest.mark.parametrize("word,expected", [("fast", "priority"), ("ultrafast", "ultrafast"),
                                         ("normal", None), ("off", None), ("", None)])
def test_explicit_policy_owns_legacy_paid_fields(config, word, expected):
    config["auxiliary"]["compression"]["service_tier"] = word
    result = build(extra_body={"service_tier": "priority", "speed": "fast", "keep": 7})
    body = result.get("extra_body", {})
    assert body.get("service_tier") == expected
    assert "speed" not in body
    assert body["keep"] == 7


@pytest.mark.parametrize("word", ["prioirty", "auto", "cold", False, True, 1, [], "ultra", "on"])
def test_bad_config_has_actionable_diagnostic(config, word):
    config["auxiliary"]["compression"]["service_tier"] = word
    with pytest.raises(ValueError, match=r"auxiliary\.compression\.service_tier"):
        build()


@pytest.mark.parametrize("entry", [route(tiers=None), route(tiers=()),
    route(models={MODEL: {"service_tiers": []}}), route(enabled=False),
    route(base_url="https://other.example/v1"), route(mode="codex_responses")])
def test_capability_omission_overrides_legacy_paid_fields(config, entry):
    config["providers"]["paid"] = entry
    body = build(extra_body={"service_tier": "priority", "speed": "fast", "keep": 7})["extra_body"]
    assert body == {"keep": 7}


@pytest.mark.parametrize("value", [None, "missing"])
def test_unset_policy_preserves_legacy_extras(config, value):
    if value == "missing":
        config["auxiliary"]["compression"].pop("service_tier")
    else:
        config["auxiliary"]["compression"]["service_tier"] = None
    assert build(extra_body={"service_tier": "priority", "speed": "fast"})["extra_body"] == {
        "service_tier": "priority", "speed": "fast"}


class CaptureClient:
    def __init__(self, *, async_mode=False, effective="custom", url=URL):
        self.base_url, self.api_key = url, "test-only"
        self._hermes_aux_effective_provider = effective
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=self.acreate if async_mode else self.create))

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return SimpleNamespace(model=kwargs["model"], choices=[SimpleNamespace(
            message=SimpleNamespace(content="summary", tool_calls=None), finish_reason="stop")])

    async def acreate(self, **kwargs):
        return self.create(**kwargs)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("selection", ["custom:paid", "paid", "main", "auto"])
def test_real_call_preserves_selected_named_identity(config, monkeypatch, async_mode, selection):
    client = CaptureClient(async_mode=async_mode)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, MODEL))
    if selection in {"main", "auto"}:
        provider = None if selection == "auto" else "main"
        config["auxiliary"]["compression"]["provider"] = selection
        runtime = {"provider": "custom", "requested_provider": "custom:paid", "model": MODEL,
                   "base_url": URL, "api_mode": "chat_completions"}
    else:
        provider, runtime = selection, {}
    kw = dict(task="compression", provider=provider, main_runtime=runtime,
              messages=[{"role": "user", "content": "summarize"}])
    if async_mode:
        asyncio.run(aux.async_call_llm(**kw))
    else:
        aux.call_llm(**kw)
    assert client.requests[0]["extra_body"]["service_tier"] == "priority"


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("tier", ["priority", "ultrafast"])
def test_anthropic_serialization_uses_speed_and_required_beta(config, monkeypatch, async_mode, tier):
    model = "claude-opus-4-6"
    config["auxiliary"]["compression"]["service_tier"] = tier
    config["providers"]["paid"] = route(mode="anthropic_messages", models=[model])
    captured = []
    def send(_client, kwargs, **_):
        captured.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="summary")],
                               stop_reason="end_turn", usage=None)
    monkeypatch.setattr("agent.anthropic_adapter.create_anthropic_message", send)
    client = aux.AnthropicAuxiliaryClient(SimpleNamespace(), model, "test", URL)
    if async_mode:
        client = aux.AsyncAnthropicAuxiliaryClient(client)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, model))
    kw = dict(task="compression", provider="custom:paid", messages=[{"role": "user", "content": "summary"}])
    if async_mode:
        asyncio.run(aux.async_call_llm(**kw))
    else:
        aux.call_llm(**kw)
    body, headers = captured[0].get("extra_body", {}), captured[0].get("extra_headers", {})
    assert "service_tier" not in body
    if tier == "priority":
        assert body["speed"] == "fast"
        assert "fast-mode-2026-02-01" in headers["anthropic-beta"]
    else:
        assert "speed" not in body
        assert "fast-mode-2026-02-01" not in headers.get("anthropic-beta", "")


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("retry_model", [MODEL, "unsupported-model"])
def test_credential_retry_reprojects_named_identity_and_actual_model(config, monkeypatch, async_mode, retry_model):
    first, retry = CaptureClient(async_mode=async_mode), CaptureClient(async_mode=async_mode)
    original = first.create
    def fail(**kwargs):
        original(**kwargs)
        err = RuntimeError("401 authentication failed")
        err.status_code = 401
        raise err
    first.create = fail
    first.chat.completions.create = first.acreate if async_mode else fail
    clients = iter([(first, MODEL), (retry, retry_model)])
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: next(clients))
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **k: True)
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route", lambda *a, **k: "custom:paid")
    config["providers"]["paid"]["models"] = {MODEL: {}, "unsupported-model": {"service_tiers": []}}
    runtime = {"provider": "custom", "requested_provider": "custom:paid", "model": MODEL,
               "base_url": URL, "api_mode": "chat_completions"}
    kw = dict(task="compression", main_runtime=runtime, messages=[{"role": "user", "content": "summary"}])
    if async_mode:
        asyncio.run(aux.async_call_llm(**kw))
    else:
        aux.call_llm(**kw)
    assert first.requests[0]["extra_body"]["service_tier"] == "priority"
    assert retry.requests[0].get("extra_body", {}).get("service_tier") == ("priority" if retry_model == MODEL else None)


@pytest.mark.parametrize("async_mode", [False, True])
def test_fallback_request_uses_its_protocol_and_not_primary_projection(config, monkeypatch, async_mode):
    config["providers"]["paid"] = route(mode="anthropic_messages", models=["claude-opus-4-6"])
    captured = []
    def send(_client, kwargs, **_):
        captured.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="summary")],
                               stop_reason="end_turn", usage=None)
    monkeypatch.setattr("agent.anthropic_adapter.create_anthropic_message", send)
    client = aux.AnthropicAuxiliaryClient(SimpleNamespace(), "claude-opus-4-6", "test", URL)
    client._hermes_fallback_destination = aux._FallbackDestination("custom:paid", URL, "anthropic_messages", "claude-opus-4-6")
    kw = dict(task="compression", messages=[{"role": "user", "content": "summary"}], tools=None,
              temperature=None, max_tokens=None, effective_timeout=300,
              effective_extra_body={"service_tier": "priority", "keep": 7}, reasoning_config=None)
    if async_mode:
        asyncio.run(aux._call_fallback_candidate_async(aux.AsyncAnthropicAuxiliaryClient(client), "claude-opus-4-6", "custom:paid", **kw))
    else:
        aux._call_fallback_candidate_sync(client, "claude-opus-4-6", "custom:paid", **kw)
    assert captured[0]["extra_body"]["speed"] == "fast"
    assert "service_tier" not in captured[0]["extra_body"]
    assert captured[0]["extra_body"]["keep"] == 7


@pytest.mark.parametrize("bound_session,context_session,expected", [
    ("session-a", "session-a", "custom:paid"),
    ("session-a", "session-b", None),
    ("", "session-a", None),
])
def test_compressor_keeps_only_its_explicit_session_route_identity(
    config, monkeypatch, bound_session, context_session, expected,
):
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(model=MODEL, provider="custom", base_url=URL,
        api_key="test-only", api_mode="chat_completions", quiet_mode=True,
        config_context_length=100000)
    compressor._session_id = bound_session
    captured = []
    monkeypatch.setattr("agent.context_compressor.call_llm", lambda **kwargs: (
        captured.append(kwargs) or SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="summary"), finish_reason="stop")])
    ))
    token = aux.set_runtime_main("custom", MODEL, requested_provider="custom:paid",
        base_url=URL, api_key="test-only", api_mode="chat_completions", session_id=context_session)
    try:
        compressor._call_summary_llm("summarize", 0.0)
    finally:
        aux.reset_runtime_main(token)
    assert captured[0]["main_runtime"].get("requested_provider") == expected


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("chain_kind", ["task", "main"])
@pytest.mark.parametrize("fallback_tiers", [("priority",), ()])
def test_actual_fallback_chain_rechecks_destination_capability(
    config, monkeypatch, async_mode, chain_kind, fallback_tiers,
):
    backup_url = "https://backup.example/v1"
    config["providers"]["backup"] = route(tiers=fallback_tiers, base_url=backup_url)
    entry = {"provider": "custom:backup", "model": MODEL,
             "base_url": backup_url, "api_mode": "chat_completions"}
    if chain_kind == "task":
        config["auxiliary"]["compression"]["fallback_chain"] = [entry]
    else:
        config["fallback_providers"] = [entry]
    first = CaptureClient(async_mode=async_mode)
    second = CaptureClient(async_mode=async_mode, url=backup_url)
    original = first.create
    def fail(**kwargs):
        original(**kwargs)
        error = RuntimeError("402 insufficient credits")
        error.status_code = 402
        raise error
    first.create = fail
    first.chat.completions.create = first.acreate if async_mode else fail
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (first, MODEL))
    monkeypatch.setattr(aux, "resolve_provider_client", lambda *a, **k: (second, MODEL))
    # CaptureClient already exposes an async create; don't construct a real SDK client.
    monkeypatch.setattr(aux, "_to_async_client", lambda client, model, **k: (client, model))
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda *a, **k: False)
    monkeypatch.setattr(aux, "_mark_provider_unhealthy", lambda *a, **k: None)
    runtime = {"provider": "custom", "requested_provider": "custom:paid", "model": MODEL,
               "base_url": URL, "api_mode": "chat_completions"}
    kw = dict(task="compression", main_runtime=runtime,
              messages=[{"role": "user", "content": "summary"}], extra_body={"keep": 7})
    if async_mode:
        asyncio.run(aux.async_call_llm(**kw))
    else:
        aux.call_llm(**kw)
    assert first.requests[0]["extra_body"]["service_tier"] == "priority"
    assert second.requests[0]["extra_body"].get("service_tier") == (
        "priority" if fallback_tiers else None)
    assert second.requests[0]["extra_body"]["keep"] == 7


def test_same_endpoint_sibling_and_unnamed_route_cannot_borrow_paid_authority(config):
    config["providers"]["normal"] = route(tiers=())
    for provider in ("custom:normal", "custom"):
        kwargs = aux._build_call_kwargs(provider, MODEL, [], base_url=URL,
            task="compression", api_mode="chat_completions",
            extra_body={"service_tier": "priority", "keep": 7})
        assert kwargs["extra_body"] == {"keep": 7}


def test_switching_profile_configuration_does_not_cache_tier_authority(config):
    assert build()["extra_body"]["service_tier"] == "priority"
    config["providers"]["paid"]["service_tiers"] = []
    assert "service_tier" not in build().get("extra_body", {})
    config["providers"]["paid"]["service_tiers"] = ["priority"]
    assert build()["extra_body"]["service_tier"] == "priority"


def test_other_auxiliary_tasks_ignore_compression_tier_policy(config):
    kwargs = aux._build_call_kwargs("custom:paid", MODEL, [], base_url=URL,
        task="vision", extra_body={"keep": 7})
    assert kwargs["extra_body"] == {"keep": 7}


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("recovery", ["model", "auth", "paid", "protocol"])
@pytest.mark.parametrize("policy", ["priority", None, "missing"])
def test_nous_recovery_revalidates_each_actual_destination(config, monkeypatch, async_mode, recovery, policy):
    if policy == "missing":
        config["auxiliary"]["compression"].pop("service_tier")
    else:
        config["auxiliary"]["compression"]["service_tier"] = policy
    nous_url = "https://inference-api.nousresearch.com/v1"
    healed_model = "unsupported-model"
    config["providers"]["paid"] = route(base_url=nous_url, models={
        MODEL: {}, healed_model: {"service_tiers": []}})
    first = CaptureClient(async_mode=async_mode, url=nous_url)
    retry = CaptureClient(async_mode=async_mode, url="https://changed.example/v1")
    original = first.create
    failed = False
    def fail_once(**kwargs):
        nonlocal failed
        response = original(**kwargs)
        if not failed:
            failed = True
            status, message = {"model": (404, "model not found"), "paid": (402, "insufficient credits")}.get(
                recovery, (401, "authentication failed"))
            error = RuntimeError(f"{status} {message}")
            error.status_code = status
            raise error
        return response
    first.create = fail_once
    first.chat.completions.create = first.acreate if async_mode else fail_once
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (first, MODEL))
    monkeypatch.setattr(aux, "_refresh_nous_recommended_model", lambda **k: healed_model)
    monkeypatch.setattr(aux, "_nous_portal_account_has_fresh_paid_access", lambda: True)
    captured_messages = []
    if recovery == "protocol":
        def send(_client, kwargs, **_):
            captured_messages.append(deepcopy(kwargs))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="summary")],
                                   stop_reason="end_turn", usage=None)
        monkeypatch.setattr("agent.anthropic_adapter.create_anthropic_message", send)
        retry = aux.AnthropicAuxiliaryClient(SimpleNamespace(), MODEL, "test-only", nous_url)
        if async_mode:
            retry = aux.AsyncAnthropicAuxiliaryClient(retry)
    monkeypatch.setattr(aux, "_refresh_nous_auxiliary_client", lambda **k: (retry, MODEL))
    kw = dict(task="compression", provider="custom:paid",
              messages=[{"role": "user", "content": "summary"}],
              extra_body={"keep": 7, "service_tier": "priority"})
    if async_mode:
        asyncio.run(aux.async_call_llm(**kw))
    else:
        aux.call_llm(**kw)
    assert first.requests[0]["extra_body"]["service_tier"] == "priority"
    recovered = (first.requests[1] if recovery == "model" else
                 captured_messages[0] if recovery == "protocol" else retry.requests[0])
    assert recovered["model"] == (healed_model if recovery == "model" else MODEL)
    assert recovered["extra_body"] == ({"keep": 7} if policy == "priority" else
                                       {"keep": 7, "service_tier": "priority"})
    assert "_compression_service_tiers" not in recovered


@pytest.mark.parametrize("revocation", ["model", "off"])
def test_reprojection_clears_stale_messages_fast_authorization(config, revocation):
    from agent.auxiliary_service_tiers import apply_compression_service_tier

    config["providers"]["paid"] = route(mode="anthropic_messages", models={
        MODEL: {}, "unsupported-model": {"service_tiers": []}})
    kwargs = aux._build_call_kwargs("custom:paid", MODEL, [], base_url=URL,
        task="compression", api_mode="anthropic_messages", extra_body={"keep": 7})
    assert kwargs["extra_body"]["speed"] == "fast"
    assert kwargs["_compression_service_tiers"]
    if revocation == "off":
        config["auxiliary"]["compression"]["service_tier"] = "off"
    result = apply_compression_service_tier(kwargs, config["auxiliary"]["compression"],
        provider="custom", requested_provider="custom:paid", base_url=URL,
        api_mode="anthropic_messages", model="unsupported-model" if revocation == "model" else MODEL)
    assert result["extra_body"] == {"keep": 7}
    assert "_compression_service_tiers" not in result


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("destination", ["same", "model", "endpoint"])
def test_explicit_task_credentials_keep_named_authority_on_refresh(
    config, monkeypatch, async_mode, destination,
):
    config["auxiliary"]["compression"].update(provider="custom:paid", model=MODEL,
        base_url=URL, api_key="test-only", api_mode="chat_completions")
    config["providers"]["paid"]["models"] = {
        MODEL: {}, "unsupported-model": {"service_tiers": []}}
    first = CaptureClient(async_mode=async_mode)
    retry = CaptureClient(async_mode=async_mode,
        url="https://changed.example/v1" if destination == "endpoint" else URL)
    retry_model = "unsupported-model" if destination == "model" else MODEL
    original = first.create
    def fail(**kwargs):
        original(**kwargs)
        error = RuntimeError("401 authentication failed")
        error.status_code = 401
        raise error
    first.create = fail
    first.chat.completions.create = first.acreate if async_mode else fail
    clients = iter([(first, MODEL), (retry, retry_model)])
    resolved_providers = []
    def cached(provider, *args, **kwargs):
        resolved_providers.append(provider)
        return next(clients)
    monkeypatch.setattr(aux, "_get_cached_client", cached)
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **k: True)
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route", lambda *a, **k: "custom")
    kw = dict(task="compression", messages=[{"role": "user", "content": "summary"}],
              extra_body={"keep": 7})
    if async_mode:
        asyncio.run(aux.async_call_llm(**kw))
    else:
        aux.call_llm(**kw)
    assert resolved_providers == ["custom", "custom"]
    assert first.requests[0]["extra_body"] == {"keep": 7, "service_tier": "priority"}
    assert retry.requests[0]["model"] == retry_model
    expected = {"keep": 7}
    if destination == "same":
        expected["service_tier"] = "priority"
    assert retry.requests[0]["extra_body"] == expected


@pytest.fixture
def sdk_chat_client(async_mode):
    import httpx
    from openai import AsyncOpenAI, OpenAI

    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-recovery-test", "object": "chat.completion", "created": 0,
            "model": MODEL, "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "summary"}}],
        })
    transport = httpx.MockTransport(respond)
    if async_mode:
        async_client = AsyncOpenAI(api_key="test-only", base_url=URL, max_retries=0,
            http_client=httpx.AsyncClient(transport=transport))
        try:
            yield async_client, requests
        finally:
            asyncio.run(async_client.close())
    else:
        with OpenAI(api_key="test-only", base_url=URL, max_retries=0,
                    http_client=httpx.Client(transport=transport)) as client:
            yield client, requests


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("original_mode", ["codex_responses", "anthropic_messages"])
def test_actual_chat_sdk_protocol_overrides_stale_hint(sdk_chat_client, async_mode, original_mode):
    client, _ = sdk_chat_client
    assert aux._aux_client_api_mode(client, original_mode) == "chat_completions"
    # Custom clients still need their configured protocol; specialized wrappers win too.
    fake = CaptureClient(async_mode=async_mode)
    assert aux._aux_client_api_mode(fake, original_mode) == original_mode
    assert aux._aux_client_api_mode(fake) == "chat_completions"
    wrapper = (aux.CodexAuxiliaryClient(fake, MODEL) if original_mode == "codex_responses"
               else aux.AnthropicAuxiliaryClient(fake, MODEL, "test-only", URL))
    if async_mode:
        wrapper = (aux.AsyncCodexAuxiliaryClient(wrapper) if original_mode == "codex_responses"
                   else aux.AsyncAnthropicAuxiliaryClient(wrapper))
    assert aux._aux_client_api_mode(wrapper, "chat_completions") == original_mode


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("original_mode", ["codex_responses", "anthropic_messages"])
def test_recovery_to_actual_chat_sdk_revokes_old_protocol_tier(
    config, sdk_chat_client, async_mode, original_mode,
):
    import json

    config["providers"]["paid"] = route(mode=original_mode)
    primary_client = CaptureClient(async_mode=async_mode)
    primary_client = (aux.CodexAuxiliaryClient(primary_client, MODEL)
        if original_mode == "codex_responses" else
        aux.AnthropicAuxiliaryClient(primary_client, MODEL, "test-only", URL))
    if async_mode:
        primary_client = (aux.AsyncCodexAuxiliaryClient(primary_client)
            if original_mode == "codex_responses" else aux.AsyncAnthropicAuxiliaryClient(primary_client))
    kwargs = aux._build_call_kwargs("custom:paid", MODEL,
        [{"role": "user", "content": "summarize"}], base_url=URL,
        task="compression", api_mode=original_mode, extra_body={"keep": 7})
    if original_mode == "codex_responses":
        assert kwargs["extra_body"]["service_tier"] == "priority"
    else:
        assert kwargs["extra_body"]["speed"] == "fast"
        assert kwargs["_compression_service_tiers"]
    req = aux._PreparedAuxRequest(
        client=primary_client, final_model=MODEL, kwargs=kwargs,
        resolved_provider="custom:paid", request_provider="custom:paid",
        resolved_model=MODEL, resolved_base_url=URL, resolved_api_key="test-only",
        resolved_api_mode=original_mode, effective_timeout=300,
        effective_extra_body={"keep": 7}, base_info=URL, requested_provider="custom:paid")
    client, requests = sdk_chat_client
    kind, (actual_client, recovered), _ = aux._ladder_step_call(
        aux._LadderStep("call", (client, kwargs)), req, {}, {"task": "compression"})
    assert kind == "call"
    assert actual_client is client
    if async_mode:
        response = asyncio.run(actual_client.chat.completions.create(**recovered))
    else:
        response = actual_client.chat.completions.create(**recovered)
    assert response.choices[0].message.content == "summary"
    assert len(requests) == 1
    assert str(requests[0].url) == URL + "/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == MODEL
    assert body["keep"] == 7
    assert not {"service_tier", "speed", "_compression_service_tiers"} & body.keys()
    assert "_compression_service_tiers" not in recovered
    # Recovery must not mutate the failed request's existing projection.
    assert kwargs["extra_body"].get("service_tier" if original_mode == "codex_responses" else "speed")
