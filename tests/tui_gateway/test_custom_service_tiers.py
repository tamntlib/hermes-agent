"""Paid-tier controls use exact profile-scoped provider routes, never URL guesses."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import hermes_yaml as yaml

from tui_gateway import server


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "tier-test"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = {
        "model": {"default": "route-model", "provider": "custom:paid"},
        "providers": {
            "unpaid": {"base_url": "https://proxy.example/v1", "api_mode": "codex_responses", "models": {"route-model": {}}},
            "paid": {"base_url": "https://proxy.example/v1", "api_mode": "codex_responses", "service_tiers": ["priority", "ultrafast"], "models": {"route-model": {}}},
        },
    }
    def save():
        (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    save()
    return home, cfg, save


def agent(**fields):
    return SimpleNamespace(**{
        "model": "route-model", "provider": "custom", "requested_provider": "custom:paid",
        "base_url": "https://proxy.example/v1", "api_mode": "codex_responses", "api_key": "",
        "service_tier": None, "request_overrides": {"keep": True}, "reasoning_config": None,
        "session_id": "key", **fields,
    })


def set_fast(home, value, live=None, **session_fields):
    session = {"agent": live, "session_key": "key", "profile_home": str(home), **session_fields}
    with patch.dict(server._sessions, {"tier-test": session}), \
            patch.object(server, "_persist_live_session_runtime"), \
            patch.object(server, "_emit_session_info"):
        result = server._methods["config.set"]("r", {"key": "fast", "value": value, "session_id": "tier-test"})
    return result, session


def test_built_agent_accepts_named_route_without_mutating_config(profile):
    home, _, _ = profile
    before = (home / "config.yaml").read_bytes()
    live = agent()
    response, session = set_fast(home, "fast", live)
    assert response.get("result", {}).get("value") == "fast", response
    assert live.service_tier == session["create_service_tier_override"] == "priority"
    assert live.request_overrides == {"keep": True, "service_tier": "priority"}
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("mode", ["fast", "ultrafast", "auto", "cold"])
def test_draft_uses_selected_named_route_offline(profile, mode):
    home, cfg, save = profile
    cfg["providers"]["paid"]["key_cmd"] = "must-not-run"
    save()
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=AssertionError("credential resolution")), \
            patch("agent.codex_service_tiers.codex_account_offers_tier", side_effect=AssertionError("account lookup")):
        response, session = set_fast(home, mode, model_override={"model": "route-model", "provider": "custom:paid"})
    assert response.get("result", {}).get("value") == mode, response
    assert session["create_service_tier_override"] == {"fast": "priority"}.get(mode, mode)


@pytest.mark.parametrize("mode", ["fast", "ultrafast", "auto", "cold"])
@pytest.mark.parametrize("live", [False, True])
def test_disabled_model_tiers_refused_before_mutation(profile, mode, live):
    home, cfg, save = profile
    cfg["providers"]["paid"]["models"]["route-model"]["service_tiers"] = []
    save()
    running = agent() if live else None
    before = (home / "config.yaml").read_bytes()
    response, session = set_fast(home, mode, running, model_override={"model": "route-model", "provider": "custom:paid"})
    assert response.get("error", {}).get("code") == 4002, response
    assert "create_service_tier_override" not in session
    if running:
        assert running.service_tier is None
        assert running.request_overrides == {"keep": True}
    assert (home / "config.yaml").read_bytes() == before


def test_bad_declaration_is_an_rpc_error_without_mutation(profile):
    home, cfg, save = profile
    cfg["providers"]["paid"]["service_tiers"] = "priority"
    save()
    response, session = set_fast(home, "fast", agent())
    assert response.get("error", {}).get("code") == 4002, response
    assert "service_tiers" in response["error"]["message"]
    assert "create_service_tier_override" not in session


@pytest.mark.parametrize("selection", [{"fast": True}, {"service_tier": "priority"}, {"service_tier": "ultrafast"}, {"service_tier": "auto"}, {"service_tier": "cold"}])
def test_create_refuses_unsupported_explicit_tier_before_session_exists(profile, selection):
    home, cfg, save = profile
    cfg["providers"]["paid"]["service_tiers"] = []
    save()
    before = (home / "config.yaml").read_bytes()
    with patch.dict(server._sessions, {}, clear=True), patch.object(server, "_schedule_agent_build"), \
            patch.object(server, "_schedule_session_cap_enforcement"):
        response = server._methods["session.create"]("r", {"profile": "tier-test", **selection})
        assert response.get("error", {}).get("code") == 4002, response
        assert not server._sessions
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("selection, expected", [({"fast": True}, "priority"), ({"service_tier": "ultrafast"}, "ultrafast"), ({"service_tier": "auto"}, "auto"), ({"service_tier": "cold"}, "cold"), ({"service_tier": "normal"}, "")])
def test_create_validates_under_owning_profile_and_keeps_session_pin(profile, selection, expected):
    home, _, _ = profile
    before = (home / "config.yaml").read_bytes()
    with patch.dict(server._sessions, {}, clear=True), patch.object(server, "_schedule_agent_build"), \
            patch.object(server, "_schedule_session_cap_enforcement"):
        response = server._methods["session.create"]("r", {"profile": "tier-test", **selection})
        assert "result" in response, response
        session = server._sessions[response["result"]["session_id"]]
        assert session["create_service_tier_override"] == expected
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("identity", ["custom:paid", "custom:unpaid", ""])
def test_runtime_persistence_preserves_exact_requested_identity(profile, identity):
    home, cfg, save = profile
    # Legacy inference may still recover undeclared entries, but cannot mint a paid identity.
    if not identity:
        cfg["providers"]["unpaid"]["service_tiers"] = []
        save()
    live = agent(requested_provider=identity)
    with server._profile_build_scope(home):
        config = server._runtime_model_config(live)
    assert config["provider"] == (identity or "custom")
    assert config["api_mode"] == "codex_responses"


def test_model_only_switch_uses_live_named_provider(profile):
    live = agent()
    provider, model, base, key = server._current_model_runtime(live, "")
    assert provider == "custom:paid"
    assert (model, base, key) == (live.model, live.base_url, live.api_key)


@pytest.mark.parametrize("primary", [False, True])
def test_one_turn_restore_keeps_named_identity_without_expanding_switch_api(profile, primary):
    live = agent()
    snapshot = server._snapshot_agent_model_runtime(live)
    assert snapshot["requested_provider"] == "custom:paid"
    live.requested_provider = "custom:unpaid"
    calls = []
    def switch_model(*, new_model, new_provider, api_key, base_url, api_mode, capabilities):
        calls.append(new_provider)
        live.requested_provider = new_provider
    live.switch_model = switch_model
    if primary:
        snapshot["primary_runtime"] = {"model": "route-model"}
        live._restore_primary_runtime = lambda: True
    server._restore_agent_model_runtime(live, snapshot)
    assert live.requested_provider == "custom:paid"
    assert calls == ([] if primary else ["custom:paid"])


@pytest.mark.parametrize("fields, expected", [({}, True), ({"requested_provider": "custom:unpaid"}, False), ({"requested_provider": ""}, False), ({"base_url": "https://other.example/v1"}, False), ({"api_mode": "chat_completions"}, False)])
def test_session_info_reports_fast_only_for_exact_live_route(profile, fields, expected):
    home, _, _ = profile
    live = agent(service_tier="priority", **fields)
    session = {"agent": live, "session_key": "key", "profile_home": str(home)}
    assert server._session_info(live, session)["fast"] is expected


def test_pending_named_route_uses_destination_endpoint_not_old_agent_url(profile):
    home, cfg, save = profile
    cfg["providers"]["paid"]["base_url"] = "https://destination.example/v1"
    save()
    live = agent(service_tier="priority", requested_provider="custom:unpaid")
    session = {"agent": live, "session_key": "key", "profile_home": str(home), "pending_model_switch": {
        "display_model": "route-model", "display_provider": "custom:paid"}}
    assert server._session_info(live, session)["fast"] is True
    session["pending_model_switch"]["display_provider"] = "custom:unpaid"
    assert server._session_info(live, session)["fast"] is False


def test_pending_model_only_pick_retains_live_named_route(profile):
    home, _, _ = profile
    live = agent(service_tier="priority")
    session = {"agent": live, "session_key": "key", "profile_home": str(home), "pending_model_switch": {
        "display_model": "next-model", "display_provider": ""}}
    assert server._session_info(live, session)["fast"] is True


@pytest.mark.parametrize("mode, expected", [("fast", False), ("auto", False), ("cold", False), ("ultrafast", True), ("normal", True)])
def test_ultrafast_only_model_replaces_provider_priority_permission(profile, mode, expected):
    home, cfg, save = profile
    cfg["providers"]["paid"]["models"]["route-model"]["service_tiers"] = ["ultrafast"]
    save()
    response, _ = set_fast(home, mode, agent())
    assert ("result" in response) is expected, response


@pytest.mark.parametrize("selection", [{"fast": False}, {"service_tier": "normal"}])
def test_create_explicit_normal_is_always_accepted_even_with_bad_tier_config(profile, selection):
    _home, cfg, save = profile
    cfg["providers"]["paid"]["service_tiers"] = "broken"
    save()
    with patch.dict(server._sessions, {}, clear=True), patch.object(server, "_schedule_agent_build"), \
            patch.object(server, "_schedule_session_cap_enforcement"):
        response = server._methods["session.create"]("r", {"profile": "tier-test", **selection})
        assert "result" in response, response
        assert server._sessions[response["result"]["session_id"]]["create_service_tier_override"] == ""


def test_draft_builtin_model_on_unopted_proxy_is_not_model_only_fast(profile):
    home, _, _ = profile
    response, session = set_fast(home, "fast", model_override={"model": "gpt-5.4", "provider": "custom:unpaid"})
    assert response.get("error", {}).get("code") == 4002, response
    assert "create_service_tier_override" not in session
