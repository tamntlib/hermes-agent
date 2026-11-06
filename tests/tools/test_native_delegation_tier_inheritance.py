"""Native dispatch inherits the live selected tier, without changing upstream SDK routing."""
import json
from pathlib import Path

import pytest

from gateway.session_authorities import owner_scope
from gateway.session_policy import policy_scope
from tests.agent.responses_test_support import response, text_item
from tests.gateway.test_session_tier_request_authority import authority_for, declaration, policy_for
from tests.tools.test_delegation_route_fields import _Backend, _parent

pytest_plugins = ("tests.agent.responses_test_support",)


@pytest.mark.parametrize("selected,explicit,expected", [
    ("priority", None, "priority"),
    (None, None, None),
    ("", None, None),
    ("priority", "normal", None),
    ("priority", "", None),
    (None, "fast", "priority"),
    ("priority", "ultrafast", "ultrafast"),
])
def test_named_route_omission_inherits_live_priority_in_real_child_request(
    make_agent, monkeypatch, tmp_path, selected, explicit, expected,
):
    from run_agent import AIAgent
    from tools import delegate_tool
    from tools.registry import registry

    make_agent()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = declaration(True, "custom:paid")
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    parent = _parent()
    parent.model, parent.provider, parent.requested_provider = "private-model", "custom", "custom:paid"
    parent.base_url, parent.api_mode = "https://tiers.invalid/v1", "codex_responses"
    parent.service_tier = selected
    parent.request_overrides = {}
    config["providers"]["paid"]["extra_body"] = {"service_tier": "ultrafast", "speed": "fast", "keep": [1]}
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    route = dict(provider="custom:paid", model="private-model", base_url=parent.base_url,
                 api_mode="codex_responses", api_key="offline-synthetic", fallback_providers=[])
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
    assert children[0].requested_provider == "custom:paid"
    assert children[0].base_url == route["base_url"] and children[0].api_mode == route["api_mode"]
    assert parent.request_overrides == {} and parent.service_tier == selected


@pytest.mark.parametrize("change", ["denied", "query", "protocol", "url_only", "auto", "cold"])
def test_unsupported_inheritance_refuses_before_any_child_effect(monkeypatch, tmp_path, change):
    from tools import delegate_tool
    from tests.tools.test_delegation_route_fields import _spawn

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = declaration(True, "custom:paid")
    config["providers"]["denied"] = {**config["providers"]["paid"], "service_tiers": []}
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    parent = _parent()
    parent.service_tier = change if change in {"auto", "cold"} else "priority"
    route = dict(provider="custom:paid", model="private-model", base_url="https://tiers.invalid/v1",
                 api_mode="codex_responses", api_key="offline-synthetic", fallback_providers=[])
    changes = {
        "denied": {"provider": "custom:denied"}, "query": {"base_url": route["base_url"] + "?tenant=other"},
        "protocol": {"api_mode": "anthropic_messages"}, "url_only": {"provider": "custom"},
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
    from hermes_cli.service_tiers import service_tier_config_scope
    from tools import delegate_tool
    from tools.delegate_tool_routes import resolve_task_routes

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = declaration(True, "custom:paid")
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    parent = _parent(request_overrides={"extra_body": {"keep": [1], "speed": "fast"}})
    parent.model, parent.provider, parent.requested_provider = "private-model", "custom", "custom:paid"
    parent.base_url, parent.api_mode = "https://tiers.invalid/v1", "codex_responses"
    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda *args: _Backend([{}, {"service_tier": "normal"}]))
    with service_tier_config_scope(config):
        for tier in ("priority", None, "ultrafast"):
            parent.service_tier = tier
            routes, error = resolve_task_routes([{"goal": "inherited"}, {"goal": "normal"}], {}, parent)
            assert error is None and not any(route.error for route in routes)
            assert routes[0].creds["service_tier"] == (tier or "")
            assert routes[1].creds["service_tier"] == ""
            routes[0].creds["request_overrides"]["extra_body"]["keep"].append(2)
            assert routes[1].creds["request_overrides"] == {"extra_body": {"keep": [1]}}
    assert parent.request_overrides == {"extra_body": {"keep": [1], "speed": "fast"}}
