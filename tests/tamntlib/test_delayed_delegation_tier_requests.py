"""A queued native child and its schema correction retain the dispatch-time wire tier."""
from tests.tools.conftest import (
    _no_host_browser_use_cli,
    _no_chromium_download,
    _no_shared_temp_socket_dirs,
    _no_host_bot_desktop_autostart,
    _materialize_mcp_sdk_symbols,
    _clear_web_result_cache,
)

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from gateway.session_authorities import owner_scope
from gateway.session_policy import policy_scope
pytest_plugins = ("tests.tamntlib.responses_test_support",)
from tests.tamntlib.delegation_test_support import authority_for, native_config, native_parent, native_route, policy_for
from tests.tools.test_delegation_route_fields import _Backend, _parent



@pytest.mark.parametrize("explicit,expected", [(None, "priority"), ("normal", None)])
def test_delayed_child_and_schema_retry_keep_dispatch_tier(make_agent, monkeypatch, tmp_path, explicit, expected):
    from run_agent import AIAgent
    from tests.tamntlib.responses_test_support import response, text_item
    from tools import delegate_tool
    from tools.delegate_tool_child_run import _validate_child_output_schema
    from tools.delegate_tool_routes import child_profile_scope
    from tools.registry import registry

    make_agent()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = native_config()
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    parent = native_parent(_parent(request_overrides={}))
    parent.service_tier = "priority"
    route = native_route()
    if explicit is not None:
        route["service_tier"] = explicit
    queued, calls = [], []
    parent_thread = threading.get_ident()

    def send(self, kwargs, **callbacks):
        assert threading.get_ident() != parent_thread
        calls.append(kwargs)
        return response(text_item('not JSON' if len(calls) == 1 else '{"ok":true}'))

    def queue_batch(batch, background):
        assert len(batch.children) == 1
        queued.append(batch.children[0][2])
        return '{"status":"queued"}'

    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda *a: _Backend([route]))
    monkeypatch.setattr(AIAgent, "_interruptible_api_call", send)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", send)
    monkeypatch.setattr(delegate_tool, "_run_batch", queue_batch)
    authority = authority_for(tmp_path)
    with owner_scope(authority), policy_scope(policy_for(tmp_path, config), authority=authority):
        result = json.loads(registry.dispatch("delegate_task", {"tasks": [{"goal": "Offline delayed child",
            "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}]},
            parent_agent=parent))
    assert result == {"status": "queued"} and not calls
    parent.service_tier = ""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "config.yaml").write_text(json.dumps(native_config("")))
    child = queued[0]
    child._cached_system_prompt = "Stable offline child prompt."
    child.compression_enabled = False
    child._cleanup_task_resources = lambda *a: None

    def run_delayed():
        from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
        token = set_hermes_home_override(foreign)
        try:
            with child_profile_scope(child):
                assert get_hermes_home() == tmp_path
                first = child.run_conversation("Offline delayed child.")
            assert first["completed"] and first["final_response"] == "not JSON"
            outcome = _validate_child_output_schema(child, first, 0, "offline-schema-retry", None)
            assert outcome.valid and outcome.retries == 1
            assert first["final_response"] == '{"ok":true}'
            assert get_hermes_home() == foreign
        finally:
            reset_hermes_home_override(token)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(run_delayed).result(timeout=30)
        assert len(calls) == 2
        assert [call.get("service_tier") for call in calls] == [expected, expected]
        assert child.requested_provider == "openai" and child.base_url == route["base_url"]
        assert child.service_tier == (expected or "") and parent.service_tier == ""
    finally:
        child.close()
