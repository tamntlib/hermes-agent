"""Owner model.options validates a live named route, not a shared URL or profile default.

Real owner admission, frozen policy, scoped config loading and inventory payload; no provider I/O.
"""
import json
from types import SimpleNamespace

import pytest

URL = 'http://127.0.0.1:1/v1'


@pytest.fixture
def home(monkeypatch):
    from gateway import run
    from hermes_constants import get_hermes_home
    profile = get_hermes_home()
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(run, '_hermes_home', profile)
    return profile


def _config():
    return {'model': {'provider': 'custom:first', 'default': 'shared-model', 'base_url': URL},
            'providers': {
                'first': {'api': URL, 'models': {'shared-model': {}}, 'service_tiers': ['priority']},
                'selected': {'api': URL, 'models': {'other-model': {}}, 'service_tiers': ['priority']}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('state,expected', [
    ('live', 'custom:selected'), ('moved', 'custom'), ('disabled', 'custom'),
    ('legacy', 'custom'), ('unpaid-legacy', 'custom'), ('unknown-url', 'custom'),
])
async def test_owner_picker_keeps_route_identity_separate_from_defaults_and_paid_authority(home, monkeypatch, state, expected):
    from gateway.session_policy import policy_for_source
    from hermes_cli import runtime_provider as rp
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    config = _config()
    if state == 'unpaid-legacy':
        for entry in config['providers'].values():
            entry.pop('service_tiers')
    (home / 'config.yaml').write_text(json.dumps(config))
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    import hermes_state_runtime as rt

    store = SessionStore(home / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                             _handle_message=None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='picker-owner', db=store._db,
                                 epoch=rt.begin_runtime_epoch(store._db, instance_id='picker-owner'))
    runner.session_authority = authority
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    try:
        created = await owner.dispatch({'id': 'create', 'method': 'session.create', 'params': {
            'request_id': 'picker', 'source': 'gui', 'cwd': str(home), 'toolsets': [],
            'provider': 'custom:selected', 'model': 'shared-model'}})
        assert 'result' in created, created
        sid = created['result']['session_id']
        live = authority.sessions[sid]
        policy_before = policy_for_source(runner, live.source).config(authority, secrets=False)
        requested = 'custom' if state in {'legacy', 'unpaid-legacy'} else 'custom:selected'
        agent = SimpleNamespace(provider='custom', requested_provider=requested, model='shared-model',
                                base_url=URL if state != 'unknown-url' else 'http://127.0.0.1:2/v1')
        runner._cached_agent_for = lambda route: agent
        if state == 'moved':
            config['providers']['selected']['api'] = 'http://127.0.0.1:2/v1'
        elif state == 'disabled':
            config['providers']['selected']['enabled'] = False
        (home / 'config.yaml').write_text(json.dumps(config))
        before = (home / 'config.yaml').read_bytes()
        # Ambient launch profile disagrees: the owner's profile must bind the executor read.
        foreign = home.parent / 'opposed-picker-profile'
        foreign.mkdir()
        (foreign / 'config.yaml').write_text(json.dumps({'providers': {
            'selected': {'api': 'http://127.0.0.1:3/v1', 'enabled': False}}}))
        token = set_hermes_home_override(foreign)
        try:
            response = await owner.dispatch({'id': 'picker', 'method': 'model.options', 'params': {
                'session_id': sid, 'include_unconfigured': True}})
        finally:
            reset_hermes_home_override(token)
        assert 'result' in response, response
        assert response['result']['provider'] == expected
        assert response['result']['model'] == 'shared-model'
        assert response['result']['providers']
        # Presentation cannot rewrite the frozen route or confer paid permission to a bare adapter.
        assert policy_for_source(runner, live.source).config(authority, secrets=False) == policy_before
        assert rp.canonical_custom_identity(base_url=URL, model='shared-model', requested='custom:selected') is None
        assert (home / 'config.yaml').read_bytes() == before
        refused = await owner.dispatch({'id': 'foreign', 'method': 'model.options', 'params': {
            'session_id': sid, 'profile': 'foreign'}})
        assert refused['error']['message'] == 'profile_mismatch'
    finally:
        await owner.close()
        store.close_all_db_handles()
