"""Native dispatch inherits the live selected tier, without changing upstream SDK routing."""
from tests.tools.conftest import (
    _no_host_browser_use_cli,
    _no_chromium_download,
    _no_shared_temp_socket_dirs,
    _no_host_bot_desktop_autostart,
    _materialize_mcp_sdk_symbols,
    _clear_web_result_cache,
)

import json
from pathlib import Path

import pytest

from gateway.session_authorities import owner_scope
from gateway.session_policy import policy_scope
pytest_plugins = ("tests.tamntlib.responses_test_support",)
from tests.tamntlib.delegation_test_support import authority_for, native_config, native_parent, native_route, policy_for
from tests.tools.test_delegation_route_fields import _Backend, _parent



@pytest.mark.parametrize("selected,explicit,expected", [
    ("priority", None, "priority"),
    ("ultrafast", None, "ultrafast"),
    (None, None, None),
    ("", None, None),
    ("priority", "normal", None),
    ("priority", "", None),
    (None, "fast", "priority"),
    ("priority", "ultrafast", "ultrafast"),
])
def test_native_route_omission_inherits_live_priority_in_real_child_request(
    make_agent, monkeypatch, tmp_path, selected, explicit, expected,
):
    from run_agent import AIAgent
    from tests.tamntlib.responses_test_support import response, text_item
    from tools import delegate_tool
    from tools.registry import registry

    make_agent()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = native_config()
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    parent = native_parent(_parent())
    parent.service_tier = selected
    parent.request_overrides = {"extra_body": {"service_tier": "ultrafast", "speed": "fast", "keep": [1]}}
    route = native_route()
    if explicit is not None:
        route["service_tier"] = explicit
    requests, children = [], []

    def send(self, kwargs, **callbacks):
        requests.append(kwargs)
        return response(text_item("Offline child completed."))

    def run_batch(batch, background):
        assert len(batch.children) == 1
        child = batch.children[0][2]
        children.append(child)
        child._cached_system_prompt = "Stable offline child prompt."
        child.compression_enabled = False
        child._cleanup_task_resources = lambda *args: None
        assert child.run_conversation("Offline child task.")["completed"]
        return '{"status":"completed"}'

    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda *a: _Backend([route]))
    monkeypatch.setattr(AIAgent, "_interruptible_api_call", send)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", send)
    monkeypatch.setattr(delegate_tool, "_run_batch", run_batch)
    authority = authority_for(tmp_path)
    with owner_scope(authority), policy_scope(policy_for(tmp_path, config), authority=authority):
        result = json.loads(registry.dispatch("delegate_task", {"tasks": [{"goal": "Offline child"}],
                                                              "background": False}, parent_agent=parent))
    assert result == {"status": "completed"}
    assert len(requests) == 1 and requests[0].get("service_tier") == expected
    assert children[0].service_tier == (expected or "")
    if expected is None:
        from tools.delegate_tool_tuning import without_service_tier
        assert children[0].request_overrides == without_service_tier(children[0].request_overrides)
    assert children[0].requested_provider == "openai"
    assert children[0].base_url == route["base_url"] and children[0].api_mode == route["api_mode"]
    assert parent.request_overrides == {"extra_body": {"service_tier": "ultrafast", "speed": "fast", "keep": [1]}}
    assert parent.service_tier == selected


@pytest.mark.parametrize("change", ["named", "private_url", "protocol", "url_only", "unsupported_model", "auto", "cold"])
def test_unsupported_inheritance_refuses_before_any_child_effect(monkeypatch, tmp_path, change):
    from tools import delegate_tool
    from tests.tools.test_delegation_route_fields import _spawn

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = native_config()
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    parent = _parent()
    parent.service_tier = change if change in {"auto", "cold"} else "priority"
    route = dict(provider="custom:paid", model="private-model", base_url="https://tiers.invalid/v1",
                 api_mode="codex_responses", api_key="offline-synthetic", fallback_providers=[])
    changes = {
        "named": {"model": "gpt-6-astra"},
        "private_url": {"provider": "openai", "model": "gpt-6-astra"},
        "protocol": {"api_mode": "anthropic_messages"}, "url_only": {"provider": "custom"},
        "unsupported_model": {**native_route(), "model": "gpt-5", "service_tier": "ultrafast"},
        "auto": {}, "cold": {},
    }
    route.update(changes[change])

    def forbidden(*args, **kwargs):
        pytest.fail("refused task reached a child construction/publication effect")

    for name in ("_open_child_session_db", "_reserve_child_transcript", "_attach_child"):
        monkeypatch.setattr(delegate_tool, name, forbidden)
    with owner_scope(authority_for(tmp_path)), policy_scope(policy_for(tmp_path, config), authority=authority_for(tmp_path)):
        result, built, children = _spawn([route], parent=parent)
    assert not built and not children
    assert ("paid window" if change in {"auto", "cold"} else "not available") in json.dumps(result)


def test_each_dispatch_reads_parent_selection_and_keeps_siblings_independent(monkeypatch, tmp_path):
    from tools import delegate_tool
    from tools.delegate_tool_routes import resolve_task_routes

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = native_config()
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    parent = native_parent(_parent(request_overrides={"extra_body": {"keep": [1], "speed": "fast"}}))
    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda *args: _Backend([{}, {"service_tier": "normal"}]))
    for tier in ("priority", None, "priority"):
        parent.service_tier = tier
        routes, error = resolve_task_routes([{"goal": "inherited"}, {"goal": "normal"}], {}, parent)
        assert error is None and not any(route.error for route in routes)
        assert routes[0].creds["service_tier"] == (tier or "")
        assert routes[1].creds["service_tier"] == ""
        routes[0].creds["request_overrides"]["extra_body"]["keep"].append(2)
        assert routes[1].creds["request_overrides"] == {"extra_body": {"keep": [1]}}
    assert parent.request_overrides == {"extra_body": {"keep": [1], "speed": "fast"}}


@pytest.mark.parametrize("tier, model", [("fast", "gpt-6-astra"), ("ultrafast", "gpt-6.1-sol")])
def test_task_tier_uses_parent_native_gate_signature(tier, model):
    """Real parent gate rejects removed custom-route kwargs; the route's own account is retained."""
    from tools.delegate_tool_tuning import task_service_tier

    from tests.tools.test_delegation_route_fields import CODEX
    from unittest.mock import patch

    runtime = {**CODEX, "model": model, "provider": "openai-codex",
               "requested_provider": "openai-codex", "api_mode": "codex_responses"}
    with patch("agent.codex_service_tiers.codex_account_offers_tier", return_value=True) as offers:
        result = task_service_tier(tier, {}, _parent(), runtime=runtime)
    assert result["task_tier_overrides"] == {"service_tier": "priority" if tier == "fast" else tier}
    if tier == "ultrafast":
        assert offers.call_args.kwargs["access_token"] == CODEX["api_key"]
