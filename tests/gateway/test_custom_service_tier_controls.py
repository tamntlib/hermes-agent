"""Gateway Fast picks are validated against the current session route at commit time."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _setup(monkeypatch, supported):
    from agent import codex_service_tiers
    from hermes_cli import models

    runtime = {
        "provider": "custom", "requested_provider": "work-proxy", "api_key": "test-key",
        "base_url": "https://proxy.invalid/v1", "api_mode": "codex_responses",
    }
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._service_tier = None
    runner._normalize_source_for_session_key = lambda source: source
    runner._resolve_session_agent_runtime = lambda **kwargs: ("private-model", dict(runtime))
    runner._evict_cached_agent = lambda key: None
    runner._try_send_choice_picker = AsyncMock(return_value=False)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", dict)
    monkeypatch.setattr(codex_service_tiers, "ultrafast_offered", lambda *a, **k: True)

    def gate(model, **route):
        if (route.get("requested_provider") == "work-proxy"
                and route.get("api_mode") == "codex_responses"
                and (route.get("tier") or "priority") in supported):
            return {"service_tier": route.get("tier") or "priority"}
        return None

    monkeypatch.setattr(models, "resolve_fast_mode_overrides", gate)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="user")
    return runner, source


def _event(source, command):
    return MessageEvent(text=command, source=source, message_id="m1")


@pytest.mark.asyncio
async def test_ultrafast_only_gateway_route_can_select_ultrafast(monkeypatch):
    runner, source = _setup(monkeypatch, {"ultrafast"})
    reply = await runner._handle_fast_command(_event(source, "/fast ultrafast"))
    assert "ULTRAFAST" in reply
    assert runner._service_tier == "ultrafast"


@pytest.mark.asyncio
async def test_gateway_picker_offers_only_eligible_tiers(monkeypatch):
    runner, source = _setup(monkeypatch, {"ultrafast"})
    await runner._handle_fast_command(_event(source, "/fast"))
    choices = runner._try_send_choice_picker.call_args.kwargs["choices"]
    assert {choice["value"] for choice in choices} == {"normal", "ultrafast"}


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["fast", "auto", "cold"])
async def test_gateway_rejects_priority_selection_on_ultrafast_only_route(monkeypatch, selection):
    runner, source = _setup(monkeypatch, {"ultrafast"})
    reply = await runner._handle_fast_command(_event(source, f"/fast {selection}"))
    assert "only available" in reply
    assert runner._service_tier is None
    assert not runner._session_service_tier_overrides


@pytest.mark.asyncio
async def test_stale_gateway_picker_rechecks_paid_tier_before_mutation(monkeypatch):
    supported = {"priority", "ultrafast"}
    runner, source = _setup(monkeypatch, supported)
    await runner._handle_fast_command(_event(source, "/fast"))
    callback = runner._try_send_choice_picker.call_args.kwargs["on_choice_selected"]
    supported.clear()
    reply = await callback(source.chat_id, "fast")
    assert "only available" in reply
    assert runner._service_tier is None
    assert not runner._session_service_tier_overrides


@pytest.mark.asyncio
async def test_gateway_normal_and_status_work_without_paid_tiers(monkeypatch):
    runner, source = _setup(monkeypatch, set())
    runner._service_tier = "priority"
    reply = await runner._handle_fast_command(_event(source, "/fast normal"))
    assert "NORMAL" in reply
    assert runner._service_tier is None
    reply = await runner._handle_fast_command(_event(source, "/fast status"))
    assert "normal" in reply.lower()


def test_turn_persistence_keeps_named_requested_provider(monkeypatch):
    import json
    from gateway.run_turn import GatewayTurnMixin

    monkeypatch.setattr("hermes_cli.runtime_provider.load_config", lambda: {"providers": {
        "work-proxy": {"base_url": "https://proxy.invalid/v1", "api_mode": "codex_responses"},
    }})

    db = MagicMock()
    db.get_session.return_value = {"model": "old-model", "model_config": "{}"}
    runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))
    agent = SimpleNamespace(
        model="private-model", provider="custom", requested_provider="work-proxy",
        base_url="https://proxy.invalid/v1", api_mode="codex_responses",
    )
    GatewayTurnMixin._sync_session_model_from_agent(runner, "session-id", agent)
    config = json.loads(db.update_session_meta.call_args.args[1])
    assert config["gateway_runtime"]["provider"] == "work-proxy"


def test_legacy_named_model_override_supplies_requested_provider(monkeypatch):
    from gateway.run_turn import GatewayTurnMixin

    override = {
        "model": "private-model", "provider": "work-proxy", "api_key": "test-key",
        "base_url": "https://proxy.invalid/v1", "api_mode": "codex_responses",
    }
    runner = SimpleNamespace(
        _resolve_session_key_or_none=lambda *a: "session-key",
        _rehydrate_session_model_override=lambda key: None,
        _peek_session_state=lambda key: SimpleNamespace(conversation=SimpleNamespace(model_override=override)),
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda *a: "default-model")
    monkeypatch.setattr(gateway_run, "_credential_pool_for_provider", lambda *a: None)
    model, runtime = GatewayTurnMixin._resolve_session_agent_runtime(runner, session_key="session-key")
    assert model == "private-model"
    assert runtime["requested_provider"] == "work-proxy"


@pytest.mark.asyncio
async def test_recorded_model_switch_keeps_exact_route_identity(monkeypatch, tmp_path):
    from gateway.slash_commands_model import _ModelSwitchContext
    from hermes_cli.model_switch import ModelSwitchResult

    runner, source = _setup(monkeypatch, {"priority"})
    runner._session_db = None
    runner._async_session_store = SimpleNamespace(set_model_override=AsyncMock())
    result = ModelSwitchResult(
        success=True, new_model="private-model", target_provider="work-proxy",
        base_url="https://proxy.invalid/v1", api_key="test-key", api_mode="codex_responses",
    )
    ctx = _ModelSwitchContext("session-key", source, tmp_path / "config.yaml", False)
    await runner._record_model_switch(result, ctx, source=source, one_turn=False, picker=False)
    override = runner._session_model_overrides["session-key"]
    assert override["provider"] == "work-proxy"
    assert override["requested_provider"] == "work-proxy"


@pytest.mark.asyncio
async def test_declared_ultrafast_reaches_turn_and_normal_preserves_provider_overrides(monkeypatch, tmp_path):
    import hermes_yaml as yaml
    from hermes_cli import models

    gate = models.resolve_fast_mode_overrides
    runner, source = _setup(monkeypatch, {"ultrafast"})
    monkeypatch.setattr(models, "resolve_fast_mode_overrides", gate)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"work-proxy": {
        "base_url": "https://proxy.invalid/v1", "api_mode": "codex_responses",
        "service_tiers": ["ultrafast"],
    }}}), encoding="utf-8")
    await runner._handle_fast_command(_event(source, "/fast ultrafast"))
    assert runner._service_tier == "ultrafast"
    model, runtime = runner._resolve_session_agent_runtime()
    runtime["request_overrides"] = {"temperature": 0.3, "extra_body": {"tenant": "keep"}}
    route = runner._resolve_turn_agent_config("hello", model, runtime)
    assert route["request_overrides"] == {
        "service_tier": "ultrafast", "temperature": 0.3, "extra_body": {"tenant": "keep"},
    }
    await runner._handle_fast_command(_event(source, "/fast normal"))
    route = runner._resolve_turn_agent_config("hello", model, runtime)
    assert route["request_overrides"] == runtime["request_overrides"]


@pytest.mark.asyncio
async def test_gateway_fast_picker_reenters_owning_profile_scope(monkeypatch, tmp_path):
    from contextlib import contextmanager

    runner, source = _setup(monkeypatch, {"priority"})
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = lambda source: tmp_path
    entered = []
    @contextmanager
    def scope(home):
        entered.append(home)
        yield
    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", scope)
    await runner._handle_fast_command(_event(source, "/fast"))
    callback = runner._try_send_choice_picker.call_args.kwargs["on_choice_selected"]
    entered.clear()
    await callback(source.chat_id, "fast")
    assert entered == [tmp_path]
