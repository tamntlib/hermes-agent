"""Canonical compression keeps destination-local native tier and runtime authority.

Exercise frozen policy + SQLite + canonical mutation + real compressor/auxiliary request
assembly. Only the provider client is replaced; no remote provider is contacted.
"""
from tests.gateway.conftest import (
    _join_api_turn_workers,
    _bind_lark_sdk_globals_when_installed,
)

import json
from types import MethodType, SimpleNamespace

import pytest
import hermes_bootstrap  # Activate dependencies before the per-test home I/O guard.

from tests.tamntlib.test_auxiliary_compression_service_tiers import CaptureClient

URL = 'https://relay.example/v1'
MODEL = 'gpt-5.5'
NATIVE_URL = 'https://api.openai.com/v1'
KEY = 'SYNTHETIC_ONLY_KEY'


def canonical_session(tmp_path, monkeypatch, *, selected='main', main='openai-api', destination=None, pooled=True):
    from agent import auxiliary_client as aux
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session_local_recovery import local_adapter_map
    from gateway.session_policy import policy_for_source
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    import hermes_state_runtime as rt

    config = {'model': {'default': MODEL, 'provider': main, 'context_length': 10000},
              'compression': {'threshold': .90, 'threshold_tokens': None,
                              'protect_first_n': 1, 'protect_last_n': 2, 'min_tail_user_messages': 1},
              'providers': {
                  'paid': {'api': URL, 'transport': 'chat_completions', 'api_key': KEY,
                           'fast_mode': True},
                  'normal': {'api': URL, 'transport': 'chat_completions', 'api_key': KEY,
                             'fast_mode': False}},
              'auxiliary': {'compression': {'provider': selected, 'service_tier': 'priority',
                                            **(destination or {})}}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('OPENAI_API_KEY', KEY)
    (tmp_path / 'config.yaml').write_text(json.dumps(config))
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a, **kw: config)
    monkeypatch.setattr('socket.socket.connect', lambda *a, **kw: pytest.fail('network forbidden'))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
        _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
        _resolve_session_agent_runtime=lambda **kw: (MODEL, {}))
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='owner', db=db, epoch=epoch)
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    ref = create_local_session(authority, owner.actor, dict(request_id='create', source='cli', cwd=str(tmp_path),
        model=MODEL, toolsets=[], **({} if pooled else {'api_key': KEY})))
    for i in range(12):
        db.append_message(ref.session_id, 'user', f'question {i}: ' + 'saved historical detail ' * 80)
        db.append_message(ref.session_id, 'assistant', f'answer {i}: ' + 'verified historical detail ' * 80)
    runner.session_authority = authority
    runner._adapter_for_source = lambda source: local_adapter_map(authority).get(source.platform)
    runner._resolve_session_agent_runtime = MethodType(GatewayTurnMixin._resolve_session_agent_runtime, runner)
    policy = policy_for_source(runner, authority.sessions[ref.session_id].source)
    assert policy is not None and not policy.follows_profile
    # Edit the live main route after creation, but leave the auxiliary preference
    # alone: v0.23.0 deliberately reads auxiliary task preferences live.
    changed = json.loads(json.dumps(config))
    changed['model']['provider'] = 'custom:normal' if main == 'custom:paid' else 'custom:paid'
    (tmp_path / 'config.yaml').write_text(json.dumps(changed))
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a, **kw: changed)
    target_native = selected in {'main', 'auto', 'openai-api'} and main == 'openai-api'
    client = CaptureClient(url=(destination or {}).get('base_url', NATIVE_URL if target_native else URL),
                           effective='openai-api' if target_native else 'custom')
    monkeypatch.setattr(aux, '_get_cached_client', lambda *a, **kw: (client, MODEL))
    return authority, owner, ref, client


@pytest.mark.asyncio
@pytest.mark.parametrize('selected,expected', [('normal', None), ('priority', 'priority')])
async def test_live_auxiliary_tier_preference_is_independent_of_frozen_main_route(tmp_path, monkeypatch, selected, expected):
    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch)
    path = tmp_path / 'config.yaml'
    current = json.loads(path.read_text())
    current['auxiliary']['compression']['service_tier'] = selected
    path.write_text(json.dumps(current))
    try:
        reply = await manual_compress(authority, owner, ref)
        assert 'result' in reply and client.requests
        assert all(request.get('extra_body', {}).get('service_tier') == expected for request in client.requests)
    finally:
        await owner.close()


FALLBACK_DESTINATIONS = [
    ('custom:normal', URL, None),
    ('custom:paid', 'https://foreign.example/v1', None),
    ('custom:paid', URL, None),
    ('openai-api', NATIVE_URL, 'priority'),
]


def fallback_session(tmp_path, monkeypatch, provider, url):
    from agent import auxiliary_client as aux

    authority, owner, ref, first = canonical_session(tmp_path, monkeypatch,
        destination={'fallback_chain': [{'provider': provider, 'model': 'gpt-5.4',
                                        'base_url': url, 'api_mode': 'chat_completions'}]})
    second = CaptureClient(url=url, effective='openai-api' if provider == 'openai-api' else 'custom')
    original = first.create

    def failed_send(**kwargs):
        original(**kwargs)
        # A model-scoped failure permits another model on the same endpoint.
        # A billing failure now intentionally benches that endpoint in v0.23.1.
        error = RuntimeError('404 synthetic model not found')
        error.status_code = 404
        raise error

    def offline_client(**kwargs):
        assert kwargs['base_url'].rstrip('/') == url
        assert kwargs['api_key'] == KEY
        second.api_key = kwargs['api_key']
        return second

    first.chat.completions.create = failed_send
    monkeypatch.setattr(aux, '_create_openai_client', offline_client)
    return authority, owner, ref, first, second


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,url,expected', FALLBACK_DESTINATIONS)
async def test_manual_fallback_rechecks_actual_destination(tmp_path, monkeypatch, provider, url, expected):
    authority, owner, ref, first, second = fallback_session(tmp_path, monkeypatch, provider, url)
    try:
        reply = await manual_compress(authority, owner, ref)
        assert 'result' in reply, reply
        assert first.requests and second.requests
        assert all(r.get('extra_body', {}).get('service_tier') == 'priority' for r in first.requests)
        assert all(r.get('extra_body', {}).get('service_tier') == expected for r in second.requests)
    finally:
        await owner.close()


async def manual_compress(authority, owner, ref):
    before = authority.db.get_session(ref.session_id)
    return await owner.dispatch({'id': 1, 'method': 'session.mutate', 'params': {
        'session_id': ref.session_id, 'request_id': 'compress',
        'expected_revision': before['runtime_revision'], 'expected_generation': before['runtime_generation'],
        'operation': 'compress', 'payload': {}}})


@pytest.mark.asyncio
@pytest.mark.parametrize('pooled', [True, False], ids=['real-pool', 'explicit-launch-key'])
@pytest.mark.parametrize('selected', ['openai-api', 'main', 'auto'])
async def test_manual_compression_preserves_selected_native_tier(tmp_path, monkeypatch, selected, pooled):
    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch, selected=selected, pooled=pooled)
    try:
        reply = await manual_compress(authority, owner, ref)
        assert 'result' in reply, reply
        assert client.requests, 'canonical compressor never dispatched a summary'
        assert all(r.get('extra_body', {}).get('service_tier') == 'priority' for r in client.requests)
        assert len(authority.db.get_messages_as_conversation(ref.session_id)) < 24
    finally:
        await owner.close()


DENIED_DESTINATIONS = [
    ('main', 'custom:normal', None),
    ('auto', 'custom:normal', None),
    ('custom:normal', 'custom:paid', None),
    ('custom:paid', 'custom:paid', {'base_url': 'https://foreign.example/v1'}),
]


def foreign_runtime():
    from agent import auxiliary_client as aux

    return aux.set_runtime_main('custom', MODEL, requested_provider='custom:paid',
        base_url=URL, api_key=KEY, api_mode='chat_completions', session_id='foreign-session')


@pytest.mark.asyncio
@pytest.mark.parametrize('pooled', [True, False], ids=['real-pool', 'explicit-launch-key'])
@pytest.mark.parametrize('selected,main,destination', DENIED_DESTINATIONS)
async def test_manual_compression_cannot_borrow_foreign_or_sibling_tier(
    tmp_path, monkeypatch, selected, main, destination, pooled,
):
    from agent import auxiliary_client as aux

    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch,
        selected=selected, main=main, destination=destination, pooled=pooled)
    token = foreign_runtime()
    prior = aux._RUNTIME_MAIN_CONTEXT.get()
    try:
        reply = await manual_compress(authority, owner, ref)
        assert 'result' in reply, reply
        assert client.requests, 'denial must exercise an actual assembled send'
        assert all('service_tier' not in r.get('extra_body', {}) for r in client.requests)
        assert aux._RUNTIME_MAIN_CONTEXT.get() is prior
    finally:
        aux.reset_runtime_main(token)
        await owner.close()


def observe_summary_runtime(monkeypatch, *, fail=False):
    """Observe reset INSIDE the same worker context, not only its isolated caller task."""
    import asyncio
    from agent import auxiliary_client as aux
    from agent.context_compressor import ContextCompressor

    to_thread = asyncio.to_thread
    generate = ContextCompressor._generate_summary
    restorations, scopes = [], []

    async def observed_thread(func, *args, **kwargs):
        if getattr(func, '__name__', '') != 'summarize':
            return await to_thread(func, *args, **kwargs)

        def invoke():
            prior = aux._RUNTIME_MAIN_CONTEXT.get()
            try:
                return func(*args, **kwargs)
            finally:
                restorations.append(aux._RUNTIME_MAIN_CONTEXT.get() is prior)
        return await to_thread(invoke)

    def observed_generate(self, *args, **kwargs):
        scoped = aux._RUNTIME_MAIN_CONTEXT.get()
        assert isinstance(scoped, dict), 'canonical summary has no runtime authority'
        scopes.append((self._session_id, scoped.get('session_id'), scoped.get('requested_provider')))
        summary = generate(self, *args, **kwargs)
        if fail:
            raise RuntimeError('synthetic summary exception after physical send')
        return summary

    monkeypatch.setattr(asyncio, 'to_thread', observed_thread)
    monkeypatch.setattr(ContextCompressor, '_generate_summary', observed_generate)
    return restorations, scopes


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', [False, True], ids=['no-prior', 'foreign-prior'])
@pytest.mark.parametrize('fail', [False, True], ids=['success', 'summary-exception'])
async def test_manual_summary_runtime_is_owned_then_restored(tmp_path, monkeypatch, nested, fail):
    from agent import auxiliary_client as aux

    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch)
    before = authority.db.get_session(ref.session_id)
    messages = authority.db.get_messages_as_conversation(ref.session_id)
    token = foreign_runtime() if nested else aux._RUNTIME_MAIN_CONTEXT.set(None)
    prior = aux._RUNTIME_MAIN_CONTEXT.get()
    restorations, scopes = observe_summary_runtime(monkeypatch, fail=fail)
    try:
        if fail:
            with pytest.raises(RuntimeError, match='synthetic summary exception'):
                await manual_compress(authority, owner, ref)
        else:
            reply = await manual_compress(authority, owner, ref)
            assert 'result' in reply, reply
        assert client.requests, 'exception control must still reach actual summary dispatch'
        assert all(r.get('extra_body', {}).get('service_tier') == 'priority' for r in client.requests)
        assert scopes and all(s == (ref.session_id, ref.session_id, 'openai-api') for s in scopes)
        assert restorations == [True], 'worker leaked the temporary canonical runtime'
        assert aux._RUNTIME_MAIN_CONTEXT.get() is prior
        if fail:
            after = authority.db.get_session(ref.session_id)
            assert after['runtime_revision'] == before['runtime_revision']
            assert authority.db.get_messages_as_conversation(ref.session_id) == messages
            assert not authority.db.list_meta_prefix('gateway.mutation.v1.')
    finally:
        aux.reset_runtime_main(token)
        await owner.close()


@pytest.mark.asyncio
async def test_manual_summary_uses_current_target_not_logical_owner(tmp_path, monkeypatch):
    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch)
    db = authority.db
    history = db.get_messages_as_conversation(ref.session_id)
    row = db.get_session(ref.session_id)
    try:
        reset = await owner.dispatch({'id': 2, 'method': 'session.mutate', 'params': {
            'session_id': ref.session_id, 'request_id': 'reset', 'operation': 'reset', 'payload': {},
            'expected_revision': row['runtime_revision'], 'expected_generation': row['runtime_generation']}})
        target = reset['result']['target_session_id']
        assert target != ref.session_id
        db.replace_messages(target, history)
        # Auxiliary preferences remain live after a reset; runtime authority must
        # nevertheless belong to the current target rather than the logical owner.
        changed = json.loads((tmp_path / 'config.yaml').read_text())
        changed['auxiliary']['compression']['service_tier'] = 'off'
        (tmp_path / 'config.yaml').write_text(json.dumps(changed))
        restorations, scopes = observe_summary_runtime(monkeypatch)
        reply = await manual_compress(authority, owner, ref)
        assert 'result' in reply, reply
        assert client.requests and all('service_tier' not in r.get('extra_body', {}) for r in client.requests)
        assert scopes and all(s == (target, target, 'openai-api') for s in scopes)
        assert restorations == [True]
    finally:
        await owner.close()
