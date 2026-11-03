"""Actual in-process requests use their owning canonical declaration, not live files."""
import json
from types import SimpleNamespace as NS

import pytest

from gateway.session_authorities import owner_scope
from gateway.session_policy import LocalSessionPolicy, policy_scope
from tests.agent.responses_test_support import response, run_turn, text_item

pytest_plugins = ("tests.agent.responses_test_support",)


def declaration(allowed, identity="custom:paid"):
    return {
        "model": {"default": "private-model", "provider": identity,
                  "base_url": "https://tiers.invalid/v1", "api_mode": "codex_responses"},
        "providers": {"paid": {"base_url": "https://tiers.invalid/v1", "api_mode": "codex_responses",
                               "service_tiers": ["priority", "ultrafast"] if allowed else []}},
        "agent": {"service_tier": "priority"}, "compression": {"enabled": False},
    }


def authority_for(home):
    return NS(profile_id=str(home), runner=NS(config=NS(multiplex_profiles=True)),
              db=NS(db_path=home / "state.db"))


def policy_for(home, config, *, follows=False):
    return LocalSessionPolicy("gui", "desktop", str(home), "private-model", (),
                              json.dumps(config), json.dumps({"follow_profile_config": follows}), "{}")


@pytest.mark.parametrize("frozen,live", [(True, False), (False, True), (True, True), (False, False)])
@pytest.mark.parametrize("identity", ["custom:paid", "paid"])
@pytest.mark.parametrize("tier", ["priority", "ultrafast", "auto", "cold", None])
def test_request_uses_canonical_frozen_declarations(make_agent, monkeypatch, tmp_path, frozen, live, identity, tier):
    from run_agent import AIAgent

    make_agent()  # Install only the existing offline constructor seams.
    config = declaration(frozen, identity)
    (tmp_path / "config.yaml").write_text(json.dumps(declaration(live)))
    authority = authority_for(tmp_path)
    policy = policy_for(tmp_path, config)
    with owner_scope(authority), policy_scope(policy, authority=authority):
        agent = AIAgent(model="private-model", provider=identity, api_mode="codex_responses",
                        base_url="https://tiers.invalid/v1", api_key="offline-fixture",
                        service_tier=tier, quiet_mode=True, max_iterations=3,
                        skip_context_files=True, skip_memory=True, save_trajectories=False)
        agent._cached_system_prompt = "Stable offline prompt."
        agent.compression_enabled = False
        agent._cleanup_task_resources = lambda *args: None
        result, requests = run_turn(agent, monkeypatch, response(text_item("Done.")))
    assert result["completed"] and len(requests) == 1
    expected = "ultrafast" if tier == "ultrafast" else "priority"
    assert requests[0].get("service_tier") == (expected if frozen and tier else None)
    assert agent.requested_provider == identity


@pytest.mark.parametrize("frozen,live", [(True, False), (False, True)])
@pytest.mark.parametrize("same_profile", [False, True])
def test_explicit_profile_child_request_owns_live_declaration(make_agent, monkeypatch, tmp_path, frozen, live, same_profile):
    from tools.delegate_tool_routes import delegated_profile_scope

    make_agent()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    child_home = tmp_path if same_profile else tmp_path / "child-home"
    child_home.mkdir(exist_ok=True)
    (child_home / "config.yaml").write_text(json.dumps(declaration(live)))
    authority = authority_for(tmp_path)
    with owner_scope(authority), policy_scope(policy_for(tmp_path, declaration(frozen)), authority=authority):
        with delegated_profile_scope(child_home):
            from run_agent import AIAgent
            agent = AIAgent(model="private-model", provider="custom:paid", api_mode="codex_responses",
                            base_url="https://tiers.invalid/v1", api_key="offline-fixture",
                            service_tier="priority", quiet_mode=True, max_iterations=1,
                            skip_context_files=True, skip_memory=True, save_trajectories=False)
            agent._cached_system_prompt = "Offline child prompt."
            agent.compression_enabled = False
            agent._cleanup_task_resources = lambda *args: None
            result, requests = run_turn(agent, monkeypatch, response(text_item("Done.")))
    assert result["completed"] and len(requests) == 1
    assert requests[0].get("service_tier") == ("priority" if live else None)
