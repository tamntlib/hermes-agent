"""Tier validation/persistence follows the route the session actually owns."""
from types import SimpleNamespace

import pytest
import hermes_yaml as yaml

from tui_gateway import server
from tui_gateway.service_tiers import selected_route, validate_selection


@pytest.mark.parametrize('configured,launched,allowed', [
    ('unpaid', 'paid', False), ('paid', 'unpaid', True),
])
def test_model_only_draft_uses_config_provider_not_launch_provider(tmp_path, monkeypatch, configured, launched, allowed):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'model': {'default': 'Alias', 'provider': 'custom:' + configured}, 'providers': {
        'paid': {'base_url': 'https://proxy.example/v1', 'transport': 'codex_responses', 'service_tiers': ['priority']},
        'unpaid': {'base_url': 'https://proxy.example/v1', 'transport': 'codex_responses', 'service_tiers': []},
    }}
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump(config))
    monkeypatch.setenv('HERMES_TUI_PROVIDER', 'custom:' + launched)
    monkeypatch.setattr(server, '_resolve_startup_runtime', lambda: ('Alias', 'custom:' + launched))
    _, route = selected_route({'model': 'Alias'})
    assert route['requested_provider'] == 'custom:' + configured
    if allowed:
        assert validate_selection('priority', override={'model': 'Alias'}) == {'service_tier': 'priority'}
    else:
        with pytest.raises(ValueError, match='not available'):
            validate_selection('priority', override={'model': 'Alias'})


def test_voice_turn_persists_the_main_session_requested_provider(monkeypatch):
    from agent.voice_turn_route import session_runtime_view

    monkeypatch.setattr('hermes_cli.runtime_provider.load_config', lambda: {'providers': {
        'main': {'base_url': 'https://main.example/v1', 'api_mode': 'codex_responses'},
        'voice': {'base_url': 'https://voice.example/v1', 'api_mode': 'codex_responses'},
    }})

    running = SimpleNamespace(
        model='VoiceAlias', provider='custom', requested_provider='custom:voice',
        base_url='https://voice.example/v1', api_mode='codex_responses',
        service_tier=None, reasoning_config=None,
        _voice_route_state={'snapshot': {
            'model': 'MainAlias', 'provider': 'custom', 'requested_provider': 'custom:main',
            'base_url': 'https://main.example/v1', 'api_mode': 'codex_responses',
        }, 'reasoning_config': None},
    )
    assert session_runtime_view(running).requested_provider == 'custom:main'
    persisted = server._runtime_model_config(running)
    assert persisted['provider'] == 'custom:main'
    assert persisted['model'] == 'MainAlias'
    assert persisted['base_url'] == 'https://main.example/v1'
    assert running.requested_provider == 'custom:voice'
