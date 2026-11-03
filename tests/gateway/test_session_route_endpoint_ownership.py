"""Persisted named identities must still own the endpoint on resume (COR-1)."""
import json
from types import SimpleNamespace

import pytest

from gateway.run_turn import GatewayTurnMixin
from hermes_cli import runtime_provider as rp
from hermes_state import SessionDB
from tui_gateway import server


@pytest.mark.parametrize("surface", ["gateway", "tui"])
@pytest.mark.parametrize("moved", [True, False], ids=["moved-entry", "same-url-siblings"])
def test_persist_resume_keeps_endpoint_credential_owner(tmp_path, monkeypatch, surface, moved):
    old_url = "https://route.invalid/Old/v1"
    cfg = {"model": {"default": "contract-model", "provider": "custom:selected"}, "providers": {
        "old-owner": {"api": old_url, "transport": "codex_responses", "api_key": "SYNTHETIC_OLD_KEY"},
        "selected": {"api": "https://route.invalid/New/v1" if moved else old_url,
                     "transport": "codex_responses", "api_key": "SYNTHETIC_SELECTED_KEY",
                     "service_tiers": ["priority"]},
    }}
    monkeypatch.setattr(rp, "load_config", lambda: cfg)
    monkeypatch.setattr(rp, "_get_model_config", lambda: cfg["model"])
    monkeypatch.setattr(rp, "_try_resolve_from_custom_pool", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    monkeypatch.setattr("socket.socket.connect", lambda *a, **kw: pytest.fail("no HTTP send permitted"))
    agent = SimpleNamespace(model="contract-model", provider="custom", requested_provider="custom:selected",
                            base_url=old_url, api_mode="codex_responses", service_tier=None, reasoning_config={})
    expected = "custom:old-owner" if moved else "custom:selected"
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("contract", source="desktop", model=agent.model, model_config={"unrelated": "keep"})
        if surface == "gateway":
            runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))
            GatewayTurnMixin._sync_session_model_from_agent(runner, "contract", agent)
            stored = json.loads(db.get_session("contract")["model_config"])
            assert stored["unrelated"] == "keep"
            route = stored["gateway_runtime"]
            # Rehydrate the persisted route without performing inference.
            runtime = rp.resolve_runtime_provider(requested=route["provider"], target_model=agent.model)
        else:
            stored = server._runtime_model_config(agent, {"unrelated": "keep"})
            db.update_session_meta("contract", json.dumps(stored), model=agent.model)
            overrides = server._stored_session_runtime_overrides(db.get_session("contract"))
            _, runtime = server._resolve_agent_model_runtime(overrides["model_override"], overrides["provider_override"])
            route = stored
        assert route["provider"] == expected
        assert route["base_url"] == old_url
        assert "api_key" not in route
        assert runtime["requested_provider"] == expected
        assert runtime["base_url"] == old_url
        assert runtime["api_key"] == ("SYNTHETIC_OLD_KEY" if moved else "SYNTHETIC_SELECTED_KEY")


def test_url_only_persistence_cannot_authorize_declared_tiers(monkeypatch):
    url = "https://paid.invalid/v1"
    cfg = {"providers": {"paid": {"api": url, "transport": "codex_responses", "service_tiers": ["priority"]}}}
    monkeypatch.setattr(rp, "load_config", lambda: cfg)
    agent = SimpleNamespace(model="contract-model", provider="custom", requested_provider="custom", base_url=url)
    stored = server._runtime_model_config(agent)
    assert stored["provider"] == "custom"
    from hermes_cli.models import resolve_fast_mode_overrides
    assert resolve_fast_mode_overrides(agent.model, provider=stored["provider"], base_url=url,
                                       api_mode="codex_responses", tier="priority") is None
