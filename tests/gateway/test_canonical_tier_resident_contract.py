"""Resident tier controls retain route gates and Codex account entitlement."""
from pathlib import Path
from types import SimpleNamespace as NS

import hermes_yaml as yaml
import pytest

from gateway.session_contract import SessionRef
from gateway import session_tui_config as controls
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_canonical_named_tier_contract import _config
from tools.delegate_tool_tuning import task_service_tier


@pytest.fixture(autouse=True)
def offline_home(monkeypatch, tmp_path):
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("Network is forbidden in tier controls")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(socket.socket, "connect", denied)


def _connection(monkeypatch, tmp_path, agent):
    writes = []
    runner = NS(service_tier="cold", request_overrides={"temperature": 0.2},
                _resolve_session_service_tier=lambda **kw: None,
                _set_session_service_tier_override=lambda *a: writes.append(a))
    authority = NS(profile_id=str(tmp_path), runner=runner, agent=lambda ref: agent,
                   sessions={"session": NS(route="route", source=None)})
    monkeypatch.setattr(controls, "_authorize_write", lambda *a: None)
    return NS(authority=authority), SessionRef(str(tmp_path), "session"), writes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat_completions", "codex_responses"])
@pytest.mark.parametrize("selection", ["paid", "denied", "url_only", "wrong_protocol"])
async def test_resident_ultrafast_uses_exact_wire_route(monkeypatch, tmp_path, mode, selection):
    cfg = _config()
    for entry in cfg["providers"].values():
        entry["api_mode"] = mode
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    agent = NS(model="private-model", provider="custom", base_url="https://tiers.invalid/v1",
               requested_provider="custom" if selection == "url_only" else "custom:" + (
                   "denied" if selection == "denied" else "paid"),
               api_mode="anthropic_messages" if selection == "wrong_protocol" else mode,
               api_key="synthetic", service_tier="cold", request_overrides={"temperature": 0.3})
    connection, ref, writes = _connection(monkeypatch, tmp_path, agent)
    call = controls.config_set(connection, ref, {"key": "fast", "value": "ultrafast"})
    if selection == "paid":
        assert await call == {"key": "fast", "value": "ultrafast"}
        assert writes == [("route", "ultrafast")]
    else:
        with pytest.raises(RuntimeStoreError, match="fast_unavailable"):
            await call
        assert writes == []
    assert agent.service_tier == "cold"
    assert agent.request_overrides == {"temperature": 0.3}
    assert connection.authority.runner.service_tier == "cold"
    assert connection.authority.runner.request_overrides == {"temperature": 0.2}


@pytest.mark.asyncio
async def test_codex_entitlement_refuses_canonical_and_child_ultrafast(monkeypatch, tmp_path):
    from agent import codex_service_tiers, model_metadata
    from tests.agent.test_codex_service_tiers import BASE, token

    monkeypatch.setattr(codex_service_tiers, "_catalogs", {})
    key = token("synthetic-priority-only")
    fingerprint = model_metadata._codex_oauth_token_fingerprint(key, BASE)
    codex_service_tiers.remember_catalog(fingerprint, [{
        "slug": "gpt-6-astra", "service_tiers": [{"id": "priority"}],
    }])
    agent = NS(model="gpt-6-astra", provider="openai-codex", requested_provider="openai-codex",
               base_url=BASE, api_mode="codex_responses", api_key=key, request_overrides={})
    connection, ref, writes = _connection(monkeypatch, tmp_path, agent)
    assert await controls.config_set(connection, ref, {"key": "fast", "value": "fast"}) == {
        "key": "fast", "value": "fast"}
    assert writes == [("route", "priority")]
    writes.clear()
    with pytest.raises(RuntimeStoreError, match="fast_unavailable"):
        await controls.config_set(connection, ref, {"key": "fast", "value": "ultrafast"})
    assert writes == []
    for creds in ({}, {"provider": "openai-codex", "model": agent.model,
                       "base_url": BASE, "api_mode": agent.api_mode, "api_key": key}):
        assert task_service_tier("fast", creds, agent)["task_tier_overrides"] == {"service_tier": "priority"}
        with pytest.raises(ValueError, match="not available"):
            task_service_tier("ultrafast", creds, agent)
    assert agent.request_overrides == {}
