"""Selected Fast must follow the resolved transport through existing builders and SDK wire bodies."""
import json
from types import SimpleNamespace

import httpx
import pytest
from anthropic import Anthropic
from openai import OpenAI

from run_agent import AIAgent
from gateway.run import GatewayRunner
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from hermes_cli.cli_info_mixin import CLIInfoMixin


@pytest.fixture
def named_route(monkeypatch, tmp_path):
    config = {'providers': {'proxy': {'api': 'https://proxy.example/v1',
        'transport': 'codex_responses'}}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **kwargs: [])
    monkeypatch.setattr('model_tools.check_toolset_requirements', dict)
    (tmp_path / 'config.yaml').write_text(json.dumps(config))
    return config


def runtime():
    return {'provider': 'custom', 'requested_provider': 'custom:proxy',
            'base_url': 'https://proxy.example/v1', 'api_mode': 'codex_responses', 'api_key': None}


@pytest.mark.parametrize('builder', ['cli', 'gateway'])
@pytest.mark.parametrize('mode', ['chat_completions', 'codex_responses', 'anthropic_messages'])
@pytest.mark.parametrize('tier', ['priority', None])
@pytest.mark.parametrize('legacy_flag', [True, False, None])
def test_selected_identity_reaches_sdk_wire(named_route, tmp_path, builder, mode, tier, legacy_flag):
    named_route['providers']['proxy']['transport'] = mode
    if legacy_flag is not None:
        named_route['providers']['proxy']['fast_mode'] = legacy_flag
    (tmp_path / 'config.yaml').write_text(json.dumps(named_route))
    route_runtime = {**runtime(), 'api_mode': mode}
    if builder == 'cli':
        owner = SimpleNamespace(model='arbitrary-alias', service_tier=tier,
                                acp_command=None, acp_args=[], **route_runtime)
        route = CLIAgentSetupMixin._resolve_turn_agent_config(owner, 'hello')
    else:
        route = GatewayRunner._resolve_turn_agent_config(SimpleNamespace(_service_tier=tier),
                                                         'hello', 'arbitrary-alias', route_runtime)
    assert (route['request_overrides'] or {}) == ({'service_tier': 'priority'} if tier else {})
    agent = AIAgent(model=route['model'], provider=route_runtime['provider'],
        requested_provider=route_runtime['requested_provider'], base_url=route_runtime['base_url'],
        api_mode=mode, api_key='test', service_tier=tier, request_overrides=route['request_overrides'],
        quiet_mode=True, skip_context_files=True, skip_memory=True, skip_background_review=True,
        enabled_toolsets=[], reasoning_config={'enabled': False}, max_tokens=128, save_trajectories=False)
    kwargs = agent._build_api_kwargs([{'role': 'user', 'content': 'hello'}])
    expected = ({'speed': 'fast'} if mode == 'anthropic_messages' else {'service_tier': 'priority'}) if tier else {}
    captured = []
    def respond(request):
        captured.append((json.loads(request.content), dict(request.headers)))
        if mode == 'anthropic_messages':
            result = {'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'arbitrary-alias',
                      'content': [], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 0}}
        elif mode == 'codex_responses':
            result = {'id': 'resp_test', 'object': 'response', 'model': 'arbitrary-alias', 'output': []}
        else:
            result = {'id': 'chat_test', 'object': 'chat.completion', 'created': 1,
                      'model': 'arbitrary-alias', 'choices': []}
        return httpx.Response(200, json=result)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        if mode == 'anthropic_messages':
            client = Anthropic(api_key='test', base_url=route_runtime['base_url'], http_client=http)
            client.messages.create(**kwargs)
        else:
            client = OpenAI(api_key='test', base_url=route_runtime['base_url'], http_client=http)
            if mode == 'codex_responses':
                client.responses.create(**kwargs)
            else:
                client.chat.completions.create(**kwargs)
    body, headers = captured[0]
    assert {key: body[key] for key in ('service_tier', 'speed') if key in body} == expected
    assert ('fast-mode-2026-02-01' in headers.get('anthropic-beta', '')) is (bool(tier) and mode == 'anthropic_messages')
    if tier and mode == 'anthropic_messages':
        from agent.fast_mode import mark_fast_mode_unavailable
        assert mark_fast_mode_unavailable(agent)
        reduced = agent._build_api_kwargs([{'role': 'user', 'content': 'hello'}])
        assert 'speed' not in reduced.get('extra_body', {})
        assert 'fast-mode-2026-02-01' not in reduced.get('extra_headers', {}).get('anthropic-beta', '')


@pytest.mark.parametrize('mode,supported', [('chat_completions', True), ('codex_responses', True),
    ('anthropic_messages', True), ('gemini_native', True), (None, True), ('unknown', True)])
def test_cli_gate_uses_resolved_transport(named_route, mode, supported):
    cli = SimpleNamespace(agent=None, model='arbitrary-alias', **{**runtime(), 'api_mode': mode})
    assert CLIInfoMixin._fast_command_available(cli) is supported


@pytest.mark.parametrize('surface', ['slash', 'gateway_info', 'tui_info'])
def test_remaining_gates_preserve_selected_identity(named_route, surface):
    agent = SimpleNamespace(model='arbitrary-alias', service_tier='priority',
                            reasoning_config=None, **runtime())
    if surface == 'slash':
        from gateway.slash_commands_model import _fast_route_supports
        assert _fast_route_supports(agent.model, runtime())
    elif surface == 'gateway_info':
        from gateway.session_local import _reasoning_fields
        assert _reasoning_fields(agent, None, agent.model)['fast']
    else:
        from tui_gateway.server import _fast_tier_applies
        assert _fast_tier_applies(agent, agent.model, agent.provider, route_known=True, tier='priority')


@pytest.mark.parametrize('resolved', [False, True])
def test_delegation_gate_preserves_selected_identity(named_route, resolved):
    from tools.delegate_tool_tuning import task_service_tier
    parent = SimpleNamespace(model='arbitrary-alias', request_overrides={}, **runtime())
    result = task_service_tier('fast', {}, parent, runtime={'model': parent.model, **runtime()} if resolved else None)
    assert result['request_overrides'] == {'service_tier': 'priority'}


@pytest.mark.parametrize('mode', ['chat_completions', 'codex_responses', 'anthropic_messages'])
def test_inventory_uses_configured_transport(named_route, mode):
    from hermes_cli.inventory import _apply_capabilities
    named_route['providers']['proxy']['transport'] = mode
    rows = [{'slug': 'custom:proxy', 'api_url': 'https://proxy.example/v1', 'models': ['arbitrary-alias']}]
    _apply_capabilities(rows, metadata_config=named_route)
    assert rows[0]['capabilities']['arbitrary-alias']['fast']


@pytest.mark.parametrize('mode', ['chat_completions', 'codex_responses', 'anthropic_messages'])
def test_worker_uses_resolved_transport(named_route, monkeypatch, mode):
    from gateway.session_worker_construct import routing_agent_kwargs, worker_route
    named_route['agent'] = {'service_tier': 'priority'}
    policy = SimpleNamespace(effective=lambda: (named_route, 'arbitrary-alias'))
    monkeypatch.setattr('gateway.session_worker_construct.route_runtime',
                        lambda *args: ({**runtime(), 'api_mode': mode}, None))
    result = worker_route(policy, {'api_key': None, 'service_tier': 'priority'})
    from agent.fast_mode import effective_request_overrides
    controls = routing_agent_kwargs(named_route, 'arbitrary-alias', 'custom', runtime()['base_url'])
    agent = SimpleNamespace(model='arbitrary-alias', provider='custom', **controls, **result)
    assert effective_request_overrides(agent) == {'service_tier': 'priority'}


@pytest.mark.parametrize('surface', ['options', 'info'])
def test_cold_ui_uses_configured_transport(named_route, tmp_path, surface):
    named_route['model'] = {'provider': 'custom:proxy'}
    named_route['agent'] = {'service_tier': 'priority'}
    policy = SimpleNamespace(follows_profile=False, effective=lambda *args, **kwargs: (named_route, 'arbitrary-alias'))
    if surface == 'options':
        from gateway.session_tui_config import service_tier_options
        authority = SimpleNamespace(profile_id=str(tmp_path), agent=lambda ref: None)
        assert 'fast' in service_tier_options(SimpleNamespace(authority=authority), None, policy, include_ultrafast=False)
    else:
        from gateway.session_local import _reasoning_fields
        assert _reasoning_fields(None, policy, 'arbitrary-alias', override={})['fast']
