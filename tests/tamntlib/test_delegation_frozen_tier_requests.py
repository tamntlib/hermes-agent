"""Registered delegation plus real offline child requests retain session tier ownership."""
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
from tests.tamntlib.delegation_test_support import authority_for, native_config, native_parent, policy_for
pytest_plugins = ("tests.tamntlib.responses_test_support",)
from tests.tools.test_delegation_route_fields import _Backend, _parent



@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("tier", ["priority", "ultrafast", "normal"])
def test_registered_frozen_child_request(make_agent, monkeypatch, tmp_path, frozen, pinned, tier):
    from run_agent import AIAgent
    from tests.tamntlib.responses_test_support import response, text_item
    from tools import delegate_tool
    from tools.delegate_tool_routes import child_profile_scope
    from tools.registry import registry

    make_agent()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = native_config("priority" if frozen else "")
    (tmp_path / "config.yaml").write_text(json.dumps(native_config("" if frozen else "priority")))
    parent = native_parent(_parent(request_overrides={"temperature": .2}))
    route = {"service_tier": tier}
    if pinned:
        route.update(provider=parent.provider, model=parent.model, base_url=parent.base_url, api_mode=parent.api_mode)
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
    assert result == {"status": "completed"} and len(calls) == 1
    assert calls[0].get("service_tier") == (None if tier == "normal" else tier)
    assert parent.request_overrides == {"temperature": .2}
