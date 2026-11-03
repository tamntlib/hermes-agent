"""Declared Anthropic tiers must survive the real request builder and SDK serializer."""
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import hermes_yaml as yaml

from agent.chat_completion_helpers import _build_api_kwargs_for_mode
from agent.transports.anthropic import AnthropicTransport


@pytest.fixture
def custom_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    endpoint = "https://messages.example/v1"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {
        "allowed": {
            "base_url": endpoint, "transport": "anthropic_messages",
            "service_tiers": ["priority"],
            "models": {"private-alias": {}, "denied-alias": {"service_tiers": []}},
        },
        "sibling": {"base_url": endpoint, "transport": "anthropic_messages"},
    }}))
    transport = AnthropicTransport()
    return SimpleNamespace(
        model="private-alias", provider="custom", requested_provider="custom:allowed",
        base_url=endpoint, _anthropic_base_url=endpoint, api_mode="anthropic_messages",
        service_tier="priority", request_overrides={}, reasoning_config={}, tools=None,
        max_tokens=1024, _is_anthropic_oauth=False,
        _get_transport=lambda: transport, _anthropic_preserve_dots=lambda: True,
        _prepare_anthropic_messages_for_api=lambda messages: messages,
    )


def _serialized_request(agent):
    kwargs = _build_api_kwargs_for_mode(agent, [
        {"role": "system", "content": "Stable system prompt"}, {"role": "user", "content": "hi"},
    ])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": agent.model, "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    with anthropic.Anthropic(
        api_key="test-key", base_url=agent.base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        client.messages.create(**kwargs)
    return requests[0]


def test_declared_custom_priority_serializes_fast_for_exact_model_alias(custom_route):
    request = _serialized_request(custom_route)
    body = json.loads(request.content)

    assert body["model"] == "private-alias"
    assert body.get("speed") == "fast"
    assert "service_tier" not in body
    assert "fast-mode-2026-02-01" in request.headers.get("anthropic-beta", "")


@pytest.mark.parametrize("changes", [
    {"requested_provider": "custom:sibling"},
    {"model": "denied-alias"},
    {"service_tier": None},
    {"base_url": "https://other.example/v1", "_anthropic_base_url": "https://other.example/v1"},
], ids=["same-endpoint-sibling", "model-denial", "normal", "different-endpoint"])
def test_custom_routes_without_priority_authorization_omit_fast(custom_route, changes):
    for name, value in changes.items():
        setattr(custom_route, name, value)

    request = _serialized_request(custom_route)
    body = json.loads(request.content)

    assert "speed" not in body
    assert "service_tier" not in body
    assert "fast-mode-2026-02-01" not in request.headers.get("anthropic-beta", "")


def test_undeclared_custom_raw_override_keeps_native_only_guard(custom_route):
    custom_route.requested_provider = "custom:sibling"
    custom_route.model = "claude-opus-4-8"
    custom_route.service_tier = None
    custom_route.request_overrides = {"speed": "fast"}

    request = _serialized_request(custom_route)

    assert "speed" not in json.loads(request.content)
    assert "fast-mode-2026-02-01" not in request.headers.get("anthropic-beta", "")


@pytest.mark.parametrize("model, expected_speed", [
    ("claude-opus-4-8", "fast"), ("claude-opus-4-7", None),
])
def test_undeclared_native_routes_keep_existing_model_guard(custom_route, model, expected_speed):
    custom_route.provider = custom_route.requested_provider = "anthropic"
    custom_route.base_url = custom_route._anthropic_base_url = "https://api.anthropic.com"
    custom_route.model = model
    custom_route.service_tier = None
    custom_route.request_overrides = {"speed": "fast"}

    request = _serialized_request(custom_route)

    assert json.loads(request.content).get("speed") == expected_speed
    assert ("fast-mode-2026-02-01" in request.headers.get("anthropic-beta", "")) == (expected_speed == "fast")


def test_custom_tier_toggle_changes_only_speed_not_prompt_or_tools(custom_route):
    custom_route.tools = [{"type": "function", "function": {
        "name": "lookup", "description": "Look up a value",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    }}]
    payloads = []
    for tier in (None, "priority", None):
        custom_route.service_tier = tier
        payloads.append(json.loads(_serialized_request(custom_route).content))

    assert payloads[1].pop("speed") == "fast"
    assert payloads[0] == payloads[1] == payloads[2]
