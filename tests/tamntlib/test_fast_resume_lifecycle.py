"""Stored Desktop speed is a session selection, including through rebuilds."""
import pytest

from persistence.conversation.sessions import NewSession
from tui_gateway import server

pytest_plugins = ["tests.tamntlib.fast_factory_fixtures"]


def resume_saved(factory, monkeypatch, tier, path="cold", *, extra_config=None):
    factory.config["agent"]["service_tier"] = "normal" if tier == "priority" else "priority"
    factory.save()
    model_config = {
        "provider": "custom:fixture", "base_url": "https://fast-fixture.invalid/v1",
        "api_mode": "codex_responses", "service_tier": tier,
        "reasoning_config": {"enabled": True, "effort": "high"},
        **(extra_config or {}),
    }
    factory.db.sessions.create(
        NewSession(session_id="saved", source="desktop", model="diagnostic-alias", model_config=model_config),
    )
    for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_schedule_resume_hydration"):
        monkeypatch.setattr(server, name, lambda *args, **kwargs: None)
    params: dict[str, object] = {"session_id": "saved", "source": "desktop"}
    if path == "eager":
        params["eager_build"] = True
    if path == "deferred":
        params["defer_history"] = True
    response = server._methods["session.resume"]("resume", params)
    assert "error" not in response, response
    sid = response["result"]["session_id"]
    record = server._sessions[sid]
    return sid, record


def build_resumed(factory, sid, record):
    if record["agent"] is None:
        kwargs = server._deferred_build_agent_kwargs(record, factory.db)
        agent = factory.make(sid, record["session_key"], **kwargs)
        assert server._attach_built_agent(sid, record, agent)
        record["agent_ready"].set()
    else:
        agent = record["agent"]
        factory.agents.append(agent)
    return agent


@pytest.mark.parametrize("path", ["eager", "cold", "deferred"])
@pytest.mark.parametrize("tier", ["priority", "normal"])
def test_actual_resume_and_rebuild_keep_selected_tier(fast_factory, monkeypatch, path, tier):
    sid, record = resume_saved(fast_factory, monkeypatch, tier, path)
    agent = build_resumed(fast_factory, sid, record)
    expected = "priority" if tier == "priority" else None
    assert fast_factory.request(agent)[0].get("service_tier") == expected
    assert agent.requested_provider == "custom:fixture"
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    rebuilt = server._rebuild_session_agent(sid, record, session_id="saved")
    fast_factory.agents.append(rebuilt)
    assert fast_factory.request(rebuilt)[0].get("service_tier") == expected


@pytest.mark.parametrize("saved,new", [("normal", "fast"), ("priority", "normal")])
def test_prebuild_speed_pick_beats_restored_tier(fast_factory, monkeypatch, saved, new):
    sid, record = resume_saved(fast_factory, monkeypatch, saved)
    result = server._methods["config.set"]("speed", {"session_id": sid, "key": "fast", "value": new})
    assert "error" not in result, result
    agent = build_resumed(fast_factory, sid, record)
    assert fast_factory.request(agent)[0].get("service_tier") == ("priority" if new == "fast" else None)


@pytest.mark.parametrize("tier", ["priority", "normal"])
def test_rebuild_override_wins_and_new_returns_to_profile(fast_factory, monkeypatch, tier):
    sid, record = resume_saved(fast_factory, monkeypatch, tier)
    build_resumed(fast_factory, sid, record)
    opposite = "" if tier == "priority" else "priority"
    rebuilt = server._rebuild_session_agent(sid, record, session_id="saved", service_tier_override=opposite)
    fast_factory.agents.append(rebuilt)
    assert fast_factory.request(rebuilt)[0].get("service_tier") == (opposite or None)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_: None)
    server._reset_session_agent(sid, record)
    fast_factory.agents.append(record["agent"])
    assert "create_service_tier_override" not in record
    assert fast_factory.request(record["agent"])[0].get("service_tier") == (opposite or None)


def test_normal_selection_is_persisted_distinctly(fast_factory, monkeypatch):
    sid, record = resume_saved(fast_factory, monkeypatch, "priority")
    build_resumed(fast_factory, sid, record)
    result = server._methods["config.set"]("speed", {"session_id": sid, "key": "fast", "value": "normal"})
    assert "error" not in result, result
    saved = fast_factory.db.sessions.get("saved").model_config
    assert saved["service_tier"] == "normal"
    assert "service_tier" not in fast_factory.request(record["agent"])[0]


@pytest.mark.parametrize("admitted", [True, False])
def test_bot_chat_filter_does_not_mint_a_tier_pin(fast_factory, monkeypatch, admitted):
    stamp = {"model": "diagnostic-alias", "provider": "custom:fixture" if admitted else "other"}
    sid, record = resume_saved(fast_factory, monkeypatch, "priority", extra_config={
        "follow_profile_config": True, "composer_override_profile": stamp,
    })
    agent = build_resumed(fast_factory, sid, record)
    assert fast_factory.request(agent)[0].get("service_tier") == ("priority" if admitted else None)
    assert (record.get("create_service_tier_override") == "priority") is admitted
    if admitted:
        assert fast_factory.db.sessions.set_title("saved", "Bot Chat")
        record["bot_caps_seen"] = "before"
        monkeypatch.setattr("tools.bot_mode_probe.capability_fingerprint", lambda _: "after")
        server._sync_bot_capabilities(sid, record)
        assert record["agent"] is not agent
        assert record["bot_caps_seen"] == "after"
        fast_factory.agents.append(record["agent"])
        assert fast_factory.request(record["agent"])[0]["service_tier"] == "priority"


@pytest.mark.parametrize("queued", [False, True])
def test_real_model_switch_preserves_priority_and_replaces_provider_extras(fast_factory, monkeypatch, queued):
    import copy

    sid, record = resume_saved(fast_factory, monkeypatch, "priority")
    agent = build_resumed(fast_factory, sid, record)
    second = copy.deepcopy(fast_factory.config["providers"]["fixture"])
    second.update(api="https://second-fixture.invalid/v1", extra_body={"fixture_marker": "new-provider"})
    fast_factory.config["providers"]["second"] = second
    fast_factory.save()
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_: None)
    record["running"] = queued
    result = server._methods["config.set"]("model", {
        "session_id": sid, "key": "model", "value": "diagnostic-alias --provider custom:second --session",
        "confirm_expensive_model": True,
    })
    assert "error" not in result, result
    if queued:
        assert record.get("pending_model_switch")
        record["running"] = False
        server._apply_pending_model_switch(sid, record)
    assert agent.requested_provider == "custom:second"
    body, _ = fast_factory.request(agent)
    assert body["service_tier"] == "priority"
    assert body["fixture_marker"] == "new-provider"


@pytest.mark.parametrize("saved,command,expected,stored", [
    ("normal", "/fast fast", "priority", "priority"),
    ("priority", "/fast normal --session", None, "normal"),
])
def test_slash_fast_mirror_updates_wire_pin_persist_and_rebuild(
        fast_factory, monkeypatch, saved, command, expected, stored):
    sid, record = resume_saved(fast_factory, monkeypatch, saved)
    agent = build_resumed(fast_factory, sid, record)
    assert server._mirror_slash_side_effects(sid, record, command) == ""
    body, _ = fast_factory.request(agent)
    assert body.get("service_tier") == expected
    assert body["fixture_marker"] == "preserved"
    assert fast_factory.db.sessions.get("saved").model_config["service_tier"] == stored
    rebuilt = server._rebuild_session_agent(sid, record, session_id="saved")
    fast_factory.agents.append(rebuilt)
    assert fast_factory.request(rebuilt)[0].get("service_tier") == expected


def test_slash_fast_mirror_rejects_unsupported_ultrafast_without_mutating_pin(fast_factory, monkeypatch):
    sid, record = resume_saved(fast_factory, monkeypatch, "normal")
    agent = build_resumed(fast_factory, sid, record)
    before_tier = agent.service_tier
    assert server._mirror_slash_side_effects(sid, record, "/fast ultrafast") == ""
    assert record["create_service_tier_override"] == ""
    assert agent.service_tier == before_tier and not agent.service_tier
    assert "service_tier" not in fast_factory.request(agent)[0]
