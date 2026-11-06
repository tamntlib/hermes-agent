"""Registered delegation plus real offline child requests retain session tier ownership."""
import json
from pathlib import Path

import pytest

from gateway.session_authorities import owner_scope
from gateway.session_policy import policy_scope
from tests.gateway.test_session_tier_request_authority import authority_for, declaration, policy_for
from tests.agent.responses_test_support import response, text_item
from tests.tools.test_delegation_route_fields import _Backend, _parent

pytest_plugins = ("tests.agent.responses_test_support",)


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("identity", ["custom:paid", "paid"])
@pytest.mark.parametrize("tier", ["priority", "ultrafast", "normal"])
def test_registered_frozen_child_request(make_agent, monkeypatch, tmp_path, frozen, pinned, identity, tier):
    from run_agent import AIAgent
    from tools import delegate_tool
    from tools.delegate_tool_routes import child_profile_scope
    from tools.registry import registry

    make_agent()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = declaration(frozen, identity)
    (tmp_path / "config.yaml").write_text(json.dumps(declaration(not frozen, identity)))
    parent = _parent(request_overrides={"temperature": .2})
    parent.model, parent.provider, parent.requested_provider = "private-model", "custom", identity
    parent.base_url, parent.api_mode = "https://tiers.invalid/v1", "codex_responses"
    route = {"service_tier": tier}
    if pinned:
        route.update(provider=identity, model=parent.model, base_url=parent.base_url, api_mode=parent.api_mode)
    calls = []

    def send(self, kwargs, **callbacks):
        calls.append(kwargs)
        return response(text_item("Offline child completed."))

    def run_batch(batch, background):
        assert len(batch.children) == 1
        child = batch.children[0][2]
        child._cached_system_prompt = "Stable offline child prompt."
        child.compression_enabled = False
        child._cleanup_task_resources = lambda *args: None
        with child_profile_scope(child):
            result = child.run_conversation("Offline child task.")
        assert result["completed"]
        return '{"status":"completed"}'

    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda *a: _Backend([route]))
    monkeypatch.setattr(AIAgent, "_interruptible_api_call", send)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", send)
    monkeypatch.setattr(delegate_tool, "_run_batch", run_batch)
    authority = authority_for(tmp_path)
    with owner_scope(authority), policy_scope(policy_for(tmp_path, config), authority=authority):
        result = json.loads(registry.dispatch("delegate_task", {"tasks": [{"goal": "Offline child"}], "background": False}, parent_agent=parent))
    if frozen or tier == "normal":
        assert result == {"status": "completed"} and len(calls) == 1
        assert calls[0].get("service_tier") == (None if tier == "normal" else tier)
    else:
        assert "not available" in result["error"] and not calls
    assert parent.request_overrides == {"temperature": .2}
