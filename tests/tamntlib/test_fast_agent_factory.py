"""A selected static tier must reach the first Desktop request without a toggle."""
import copy

import pytest

pytest_plugins = ["tests.tamntlib.fast_factory_fixtures"]


def test_cold_priority_reaches_first_serialized_request(fast_factory):
    agent = fast_factory.make(service_tier_override="priority")
    body, _ = fast_factory.request(agent)
    assert agent.model == "diagnostic-alias"
    assert agent.service_tier == "priority"
    assert body["service_tier"] == "priority"
    assert body["fixture_marker"] == "preserved"


@pytest.mark.parametrize("tier", ["", "priority", "auto", "cold"])
def test_selection_replaces_configured_paid_overrides(fast_factory, tier):
    fast_factory.config["agent"]["service_tier"] = "priority"
    provider = fast_factory.config["providers"]["fixture"]
    provider["extra_body"].update(service_tier="default", speed="fast")
    before = copy.deepcopy(provider)
    agent = fast_factory.make(service_tier_override=tier)
    body, _ = fast_factory.request(agent)
    assert body.get("service_tier") == ("priority" if tier == "priority" else None)
    assert "speed" not in body
    assert body["fixture_marker"] == "preserved"
    assert provider == before


@pytest.mark.parametrize("mode", ["chat_completions", "codex_responses", "anthropic_messages"])
@pytest.mark.parametrize("explicit", [False, True])
def test_profile_and_explicit_priority_follow_the_resolved_transport(fast_factory, mode, explicit):
    fast_factory.config["providers"]["fixture"]["transport"] = mode
    fast_factory.config["agent"]["service_tier"] = "normal" if explicit else "priority"
    agent = fast_factory.make(**({"service_tier_override": "priority"} if explicit else {}))
    body, headers = fast_factory.request(agent)
    assert agent.request_overrides["extra_body"]["fixture_marker"] == "preserved"
    if mode == "anthropic_messages":
        assert body["speed"] == "fast"
        assert "service_tier" not in body
        assert "fast-mode-2026-02-01" in headers["anthropic-beta"]
    else:
        assert body["service_tier"] == "priority"
    normal = fast_factory.make(service_tier_override="")
    body, headers = fast_factory.request(normal)
    assert "service_tier" not in body and "speed" not in body
    assert "fast-mode-2026-02-01" not in headers.get("anthropic-beta", "")


@pytest.mark.parametrize("tier", ["auto", "cold"])
def test_bounded_modes_keep_their_window_policy(fast_factory, monkeypatch, tier):
    from agent import fast_mode

    clock = [100.0]
    monkeypatch.setattr(fast_mode.time, "monotonic", lambda: clock[0])
    agent = fast_factory.make(service_tier_override=tier)
    agent.fast_auto_seconds = 60
    assert "service_tier" not in fast_factory.request(agent)[0]
    fast_mode.begin_turn(agent, [])
    assert fast_factory.request(agent)[0]["service_tier"] == "priority"
    clock[0] += 61
    assert "service_tier" not in fast_factory.request(agent)[0]
    fast_mode.begin_turn(agent, [{"role": "user", "content": "earlier turn"}])
    assert fast_factory.request(agent)[0].get("service_tier") == ("priority" if tier == "auto" else None)


def test_no_session_tier_preserves_provider_only_override(fast_factory):
    fast_factory.config["providers"]["fixture"]["extra_body"]["service_tier"] = "priority"
    agent = fast_factory.make()
    assert fast_factory.request(agent)[0]["service_tier"] == "priority"


def test_explicit_normal_cleans_top_level_without_mutating_provider_input(fast_factory):
    from tui_gateway import server

    agent = fast_factory.make()
    original = {"service_tier": "priority", "speed": "fast", "extra_headers": {"X-Fixture": "kept"},
                "extra_body": {"service_tier": "priority", "speed": "fast", "fixture_marker": "preserved"}}
    before = copy.deepcopy(original)
    agent.request_overrides = original
    server._apply_session_service_tier(agent, agent.service_tier, explicit=True)
    body, headers = fast_factory.request(agent)
    assert "service_tier" not in body and "speed" not in body
    assert body["fixture_marker"] == "preserved" and headers["x-fixture"] == "kept"
    assert original == before


@pytest.mark.parametrize("tier", ["priority", "ultrafast"])
def test_first_party_static_tier_reaches_first_request(fast_factory, monkeypatch, tier):
    from tui_gateway import server

    # The provider/auth boundary supplies this built-in route. The ordinary
    # `openai` API alias resolves to adapter `custom`, so it is not this fixture.
    runtime = {"provider": "openai", "base_url": "https://api.openai.com/v1",
               "api_mode": "codex_responses", "api_key": "test"}
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda *_: ("gpt-6-astra", runtime))
    agent = fast_factory.make(service_tier_override=tier)
    assert agent.provider == "openai"
    assert fast_factory.request(agent)[0]["service_tier"] == tier


def test_unsupported_ultrafast_does_not_fall_back_to_paid_priority(fast_factory):
    fast_factory.config["providers"]["fixture"]["extra_body"]["service_tier"] = "priority"
    agent = fast_factory.make(service_tier_override="ultrafast")
    assert "service_tier" not in fast_factory.request(agent)[0]


@pytest.mark.parametrize("tier", ["priority", "", "auto", "cold"])
@pytest.mark.parametrize("recovery", ["transport", "turn_restore"])
def test_selected_tier_survives_primary_runtime_recovery(fast_factory, monkeypatch, tier, recovery):
    import httpx
    from agent import agent_runtime_helpers

    if tier != "priority":
        fast_factory.config["providers"]["fixture"]["extra_body"]["service_tier"] = "priority"
    agent = fast_factory.make(service_tier_override=tier)
    expected = "priority" if tier == "priority" else None
    assert fast_factory.request(agent)[0].get("service_tier") == expected
    identity = (agent.model, agent.provider, agent.requested_provider, agent.base_url)
    if recovery == "transport":
        monkeypatch.setattr(agent_runtime_helpers.time, "sleep", lambda _: None)
        assert agent._try_recover_primary_transport(httpx.ConnectError("offline fixture"), retry_count=0, max_retries=0)
    else:
        agent._fallback_activated = True
        agent.request_overrides = {"extra_body": {"fallback_marker": True}}
        assert agent._restore_primary_runtime()
    assert (agent.model, agent.provider, agent.requested_provider, agent.base_url) == identity
    assert agent.service_tier == tier
    body, _ = fast_factory.request(agent)
    assert body.get("service_tier") == expected
    assert body["fixture_marker"] == "preserved"


@pytest.mark.parametrize("initial,pick", [("priority", "normal"), ("", "fast")])
@pytest.mark.parametrize("recovery", ["transport", "turn_restore"])
def test_live_speed_pick_survives_primary_recovery(fast_factory, monkeypatch, initial, pick, recovery):
    import httpx
    from agent import agent_runtime_helpers
    from tui_gateway import server

    agent = fast_factory.make(service_tier_override=initial)
    server._sessions["manual"] = {"agent": agent, "session_key": "conversation"}
    result = server._methods["config.set"]("speed", {"session_id": "manual", "key": "fast", "value": pick})
    assert "error" not in result, result
    expected = "priority" if pick == "fast" else None
    assert fast_factory.request(agent)[0].get("service_tier") == expected
    if recovery == "transport":
        monkeypatch.setattr(agent_runtime_helpers.time, "sleep", lambda _: None)
        assert agent._try_recover_primary_transport(httpx.ConnectError("offline fixture"), retry_count=0, max_retries=0)
    else:
        agent._fallback_activated = True
        agent.request_overrides = {}
        assert agent._restore_primary_runtime()
    assert fast_factory.request(agent)[0].get("service_tier") == expected


@pytest.mark.parametrize("pick", ["fast", "ultrafast"])
def test_live_pick_keeps_primary_route_extras_and_gates_during_fallback(fast_factory, pick):
    from tui_gateway import server

    agent = fast_factory.make(service_tier_override="")
    primary_identity = (agent.model, agent.provider, agent.base_url)
    # State at the existing restore boundary: the active fallback and primary
    # snapshot intentionally have different routes, capabilities and extras.
    agent.model, agent.provider, agent.base_url = "gpt-6-astra", "openai", "https://api.openai.com/v1"
    agent.request_overrides = {"extra_body": {"fallback_marker": True},
                               "extra_headers": {"X-Fallback-Only": "fixture"}}
    agent._fallback_activated = True
    server._sessions["manual"] = {"agent": agent, "session_key": "conversation"}
    result = server._methods["config.set"]("speed", {"session_id": "manual", "key": "fast", "value": pick})
    assert "error" not in result, result
    assert agent._restore_primary_runtime()
    body, headers = fast_factory.request(agent)
    assert (agent.model, agent.provider, agent.base_url) == primary_identity
    assert body.get("service_tier") == ("priority" if pick == "fast" else None)
    assert body["fixture_marker"] == "preserved"
    assert "fallback_marker" not in body and "x-fallback-only" not in headers
