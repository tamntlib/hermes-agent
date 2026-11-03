"""Named custom Ultrafast reaches delegate_task's registered handler and actual route resolver."""
import json
from pathlib import Path

import hermes_yaml as yaml
import pytest

from tests.gateway.test_canonical_named_tier_contract import _config
from tests.tools.test_delegation_route_fields import _Backend, _construct, _parent


@pytest.mark.parametrize("mode", ["chat_completions", "codex_responses"])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("selection", ["paid", "denied", "url_only", "wrong_protocol", "wrong_endpoint"])
def test_registered_delegation_retains_exact_ultrafast_child_route(monkeypatch, tmp_path, mode, pinned, selection):
    import socket
    import run_agent
    from tools import delegate_tool
    from tools.registry import registry

    def no_network(*args, **kwargs):
        raise AssertionError("Network is forbidden in delegation route controls")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = _config()
    for entry in cfg["providers"].values():
        entry.update(api_mode=mode, api_key="offline-synthetic")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    requested = "custom" if selection == "url_only" else "custom:" + (
        "denied" if selection == "denied" else "paid")
    wire = "anthropic_messages" if selection == "wrong_protocol" else mode
    route = {"service_tier": "ultrafast"}
    parent = _parent(request_overrides={"temperature": 0.2})
    parent.model, parent.provider, parent.requested_provider = "private-model", "custom", "custom:paid"
    parent.base_url, parent.api_mode = (
        "https://other.invalid/v1" if selection == "wrong_endpoint" else "https://tiers.invalid/v1", mode)
    if pinned:
        route.update(provider=requested, model=parent.model, base_url=parent.base_url, api_mode=wire)
    else:
        parent.requested_provider, parent.api_mode = requested, wire
    built, batches = [], []

    def construct(**kwargs):
        built.append(kwargs)
        return _construct(**kwargs)

    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda *a: _Backend([route]))
    monkeypatch.setattr(run_agent, "AIAgent", construct)
    monkeypatch.setattr(delegate_tool, "_run_batch", lambda batch, background: batches.append(batch) or '{"status":"built"}')
    dispatched = registry.dispatch("delegate_task", {"tasks": [{"goal": "offline child"}]}, parent_agent=parent)
    assert isinstance(dispatched, str)
    result = json.loads(dispatched)
    if selection == "paid":
        assert result == {"status": "built"}
        assert len(built) == len(batches) == 1
        assert built[0]["service_tier"] == "ultrafast"
        assert built[0]["request_overrides"]["service_tier"] == "ultrafast"
        assert built[0]["base_url"] == parent.base_url
        assert built[0]["requested_provider"] == "custom:paid"
        assert built[0]["api_mode"] == mode
        child = batches[0].children[0][2]
        assert child.request_overrides["service_tier"] == "ultrafast"
    else:
        assert "not available" in result["error"]
        assert not built and not batches
    assert parent.request_overrides == {"temperature": 0.2}


@pytest.mark.parametrize("mode", ["chat_completions", "codex_responses"])
@pytest.mark.parametrize("selection", ["paid", "denied"])
def test_registered_unprefixed_named_route_retains_declared_tier(monkeypatch, tmp_path, mode, selection):
    import socket
    import run_agent
    from tools import delegate_tool
    from tools.registry import registry

    def no_network(*args, **kwargs):
        raise AssertionError("Network is forbidden in delegation route controls")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = _config()
    for entry in cfg["providers"].values():
        entry.update(api_mode=mode, api_key="offline-synthetic")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    parent = _parent(request_overrides={"temperature": 0.2})
    route = {"provider": selection, "model": "private-model", "base_url": "https://tiers.invalid/v1",
             "api_mode": mode, "service_tier": "ultrafast"}
    built = []

    def construct(**kwargs):
        built.append(kwargs)
        return _construct(**kwargs)

    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda *a: _Backend([route]))
    monkeypatch.setattr(run_agent, "AIAgent", construct)
    monkeypatch.setattr(delegate_tool, "_run_batch", lambda batch, background: '{"status":"built"}')
    dispatched = registry.dispatch("delegate_task", {"tasks": [{"goal": "offline child"}]}, parent_agent=parent)
    assert isinstance(dispatched, str)
    result = json.loads(dispatched)
    if selection == "paid":
        assert result == {"status": "built"}
        assert len(built) == 1
        assert built[0]["requested_provider"] == selection
        assert built[0]["api_mode"] == mode
        assert built[0]["request_overrides"]["service_tier"] == "ultrafast"
    else:
        assert "not available" in result["error"]
        assert not built
    assert parent.request_overrides == {"temperature": 0.2}
