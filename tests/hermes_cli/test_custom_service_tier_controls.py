"""Session Fast controls must use the same named route and tier as the next turn."""

from types import SimpleNamespace

import pytest

# Facades bootstrap their payload at import time, before the per-test home guard.
import cli as _cli_bootstrap
import gateway.run


def _runtime():
    return {
        "provider": "custom", "requested_provider": "work-proxy",
        "base_url": "https://proxy.invalid/v1", "api_mode": "codex_responses",
        "api_key": "test-key", "command": None, "args": [],
    }


@pytest.mark.parametrize("surface", ["cli", "gateway", "gateway_command"])
def test_static_tier_gate_receives_exact_route_identity(monkeypatch, surface):
    from hermes_cli import models

    calls = []

    def gate(model, **kwargs):
        calls.append((model, kwargs))
        return {"service_tier": "ultrafast"}

    monkeypatch.setattr(models, "resolve_fast_mode_overrides", gate)
    runtime = _runtime()
    if surface == "cli":
        from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
        cli = SimpleNamespace(
            **{k: v for k, v in runtime.items() if k not in ("command", "args")},
            model="private-model", service_tier="ultrafast", acp_command=None, acp_args=[],
        )
        CLIAgentSetupMixin._resolve_turn_agent_config(cli, "hello")
    elif surface == "gateway":
        from gateway.run_turn import GatewayTurnMixin
        GatewayTurnMixin._resolve_turn_agent_config(
            SimpleNamespace(_service_tier="ultrafast"), "hello", "private-model", runtime,
        )
    else:
        from gateway.slash_commands_model import _fast_route_supports
        assert _fast_route_supports("private-model", runtime, "ultrafast")
    assert len(calls) == 1
    assert calls[0][1]["requested_provider"] == "work-proxy"
    assert calls[0][1]["api_mode"] == "codex_responses"
    assert calls[0][1]["tier"] == "ultrafast"


def _cli_shell(monkeypatch, supported):
    from hermes_cli import cli_commands_mixin as commands, cli_info_mixin as info, models
    from agent import codex_service_tiers

    output = []
    monkeypatch.setattr(commands, "_cp", lambda *lines: output.extend(lines))
    monkeypatch.setattr(commands, "_accent_line", str)
    monkeypatch.setattr(commands, "_dim_line", str)
    monkeypatch.setattr(commands, "_retire_agent", lambda cli: setattr(cli, "agent", None))
    monkeypatch.setattr(codex_service_tiers, "ultrafast_offered_for", lambda *a, **k: True)

    def gate(model, **route):
        if (route.get("requested_provider") == "work-proxy"
                and route.get("api_mode") == "codex_responses"
                and (route.get("tier") or "priority") in supported):
            return {"service_tier": route.get("tier") or "priority"}
        return None

    monkeypatch.setattr(models, "resolve_fast_mode_overrides", gate)
    class CLI(commands.CLICommandsMixin, info.CLIInfoMixin):
        pass
    cli = CLI()
    cli.__dict__.update(_runtime(), model="private-model", service_tier=None, agent=None)
    return cli, output


@pytest.mark.parametrize("selection", ["fast", "auto", "cold"])
def test_cli_rejects_priority_selection_on_ultrafast_only_route(monkeypatch, selection):
    cli, output = _cli_shell(monkeypatch, {"ultrafast"})
    cli.service_tier = "ultrafast"
    cli._handle_fast_command(f"/fast {selection}")
    assert cli.service_tier == "ultrafast"
    assert "only available" in " ".join(output)


def test_cli_ultrafast_only_route_can_select_ultrafast(monkeypatch):
    cli, _output = _cli_shell(monkeypatch, {"ultrafast"})
    assert cli._fast_command_available()
    cli._handle_fast_command("/fast ultrafast")
    assert cli.service_tier == "ultrafast"


def test_cli_normal_and_status_remain_available_without_paid_tiers(monkeypatch):
    cli, output = _cli_shell(monkeypatch, set())
    cli.service_tier = "priority"
    assert cli._command_available("/fast")
    cli._handle_fast_command("/fast normal")
    assert cli.service_tier is None
    output.clear()
    cli._handle_fast_command("/fast status")
    assert ": normal" in output[0]


@pytest.mark.parametrize("command", ["/fast fast", "/fast status"])
def test_cli_reports_invalid_declarations_without_mutation(monkeypatch, command):
    from hermes_cli import models

    cli, output = _cli_shell(monkeypatch, {"priority"})
    cli.service_tier = "priority"
    def invalid(*args, **kwargs):
        raise ValueError("providers.work-proxy.service_tiers must be a list")
    monkeypatch.setattr(models, "resolve_fast_mode_overrides", invalid)
    cli._handle_fast_command(command)
    assert cli.service_tier == "priority"
    assert "providers.work-proxy.service_tiers" in " ".join(output)
    assert "currently: fast" not in " ".join(output).lower()


def test_cli_status_does_not_claim_fast_on_an_ineligible_route(monkeypatch):
    cli, output = _cli_shell(monkeypatch, set())
    cli.service_tier = "priority"
    cli._handle_fast_command("/fast status")
    assert ": normal" in output[0]


@pytest.mark.parametrize("surface", ["cli", "gateway"])
def test_turn_reports_invalid_service_tier_configuration(monkeypatch, caplog, surface):
    from hermes_cli import models

    def invalid(*args, **kwargs):
        raise ValueError("providers.work-proxy.service_tiers must be a list")
    monkeypatch.setattr(models, "resolve_fast_mode_overrides", invalid)
    if surface == "cli":
        from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
        runtime = _runtime()
        shell = SimpleNamespace(
            **{k: v for k, v in runtime.items() if k not in ("command", "args")},
            model="private-model", service_tier="priority", acp_command=None, acp_args=[],
        )
        CLIAgentSetupMixin._resolve_turn_agent_config(shell, "hello")
    else:
        from gateway.run_turn import GatewayTurnMixin
        GatewayTurnMixin._resolve_turn_agent_config(
            SimpleNamespace(_service_tier="priority"), "hello", "private-model", _runtime(),
        )
    assert "providers.work-proxy.service_tiers" in caplog.text
