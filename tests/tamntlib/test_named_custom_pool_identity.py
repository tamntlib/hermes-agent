"""A named custom route keeps its selection through the real credential-pool handoff."""
from tests.hermes_cli.conftest import (
    _suppress_concurrent_hermes_gate,
    _private_repo_checkout_lock,
    _source_channels_resolve_locally,
    _no_detached_commit_graph_writer,
    _discharge_host_update_obligation,
    _reset_prompt_toolkit_output_cache,
)

import json


def provider_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'model': {'provider': 'custom:paid', 'default': 'summary-model'},
              'providers': {'paid': {'api': 'https://relay.example/v1',
                  'transport': 'chat_completions', 'fast_mode': True,
                  'extra_headers': {'X-Synthetic': 'offline'}, 'extra_body': {'offline': True}}}}
    (tmp_path / 'config.yaml').write_text(json.dumps(config))
    rows = [{'id': 'primary', 'label': 'synthetic primary', 'auth_type': 'api_key',
             'priority': 0, 'source': 'manual', 'access_token': 'SYNTHETIC_POOL_PRIMARY',
             'account_id': 'synthetic-account', 'base_url': 'https://relay.example/v1'}]
    (tmp_path / 'auth.json').write_text(json.dumps({'version': 1, 'providers': {},
                                                 'credential_pool': {'paid': rows}}))
    monkeypatch.setattr('socket.socket.connect', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('network forbidden')))
    return config


def test_direct_named_pool_preserves_identity_and_credential_metadata(tmp_path, monkeypatch):
    from hermes_cli import runtime_provider as rp
    from hermes_cli.runtime_provider_custom import _resolve_named_custom_runtime

    config = provider_home(tmp_path, monkeypatch)
    with rp.frozen_runtime_config(config):
        runtime = _resolve_named_custom_runtime(requested_provider='custom:paid',
                                               target_model='chosen-model', config=config)
    assert runtime is not None
    assert runtime['provider'] == 'custom'
    assert runtime['base_url'] == 'https://relay.example/v1'
    assert runtime['api_mode'] == 'chat_completions'
    assert runtime['api_key'] == 'SYNTHETIC_POOL_PRIMARY'
    assert runtime['source'] == 'pool:paid'
    assert runtime['model'] == 'chosen-model'
    assert runtime['extra_headers'] == {'X-Synthetic': 'offline'}
    assert runtime['request_overrides'] == {'extra_body': {'offline': True}}
    entry = runtime['credential_pool'].select()
    assert entry is not None
    assert (entry.id, entry.source, entry.extra['account_id'], entry.access_token) == (
        'primary', 'manual', 'synthetic-account', 'SYNTHETIC_POOL_PRIMARY')
    assert runtime.get('requested_provider') == 'custom:paid'


def test_public_and_direct_resolution_agree_without_replacing_pool(tmp_path, monkeypatch):
    from hermes_cli import runtime_provider as rp
    from hermes_cli.runtime_provider_custom import _resolve_named_custom_runtime

    provider_home(tmp_path, monkeypatch)
    direct = _resolve_named_custom_runtime(requested_provider='custom:paid')
    public = rp.resolve_runtime_provider(requested='custom:paid')
    assert direct is not None
    for key in ('provider', 'requested_provider', 'base_url', 'api_mode', 'api_key', 'source'):
        assert direct[key] == public[key]
    assert direct['requested_provider'] == 'custom:paid'
    assert direct['credential_pool'].entries()[0].extra['account_id'] == 'synthetic-account'
    assert public['credential_pool'].entries()[0].extra['account_id'] == 'synthetic-account'


def test_explicit_launch_key_bypasses_real_pool_without_losing_identity(tmp_path, monkeypatch):
    from hermes_cli import runtime_provider as rp
    from hermes_cli.runtime_provider_custom import _resolve_named_custom_runtime

    config = provider_home(tmp_path, monkeypatch)
    with rp.frozen_runtime_config(config):
        for runtime in (
            _resolve_named_custom_runtime(requested_provider='custom:paid', config=config,
                                          explicit_api_key='SYNTHETIC_LAUNCH_KEY'),
            rp.resolve_runtime_provider(requested='custom:paid', explicit_api_key='SYNTHETIC_LAUNCH_KEY'),
        ):
            assert runtime is not None
            assert runtime['api_key'] == 'SYNTHETIC_LAUNCH_KEY'
            assert runtime['requested_provider'] == 'custom:paid'
            assert runtime.get('credential_pool') is None
            assert runtime['source'] == 'custom_provider:paid'


def test_real_pool_rotation_preserves_named_route_and_account(tmp_path, monkeypatch):
    from hermes_cli import runtime_provider as rp
    from hermes_cli.runtime_provider_custom import _resolve_named_custom_runtime

    config = provider_home(tmp_path, monkeypatch)
    auth_path = tmp_path / 'auth.json'
    auth = json.loads(auth_path.read_text())
    auth['credential_pool']['paid'].append({**auth['credential_pool']['paid'][0],
        'id': 'secondary', 'priority': 1, 'label': 'synthetic secondary',
        'access_token': 'SYNTHETIC_POOL_SECONDARY', 'account_id': 'synthetic-other-account'})
    auth_path.write_text(json.dumps(auth))
    with rp.frozen_runtime_config(config):
        runtime = _resolve_named_custom_runtime(requested_provider='custom:paid', config=config)
        assert runtime is not None
        pool = runtime['credential_pool']
        rotated = pool.mark_exhausted_and_rotate(status_code=401, credential_id='primary',
                                               api_key_hint='SYNTHETIC_POOL_PRIMARY')
        assert rotated is not None
        assert (rotated.id, rotated.extra['account_id']) == ('secondary', 'synthetic-other-account')
        again = _resolve_named_custom_runtime(requested_provider='custom:paid', config=config)
    assert again is not None
    assert again['api_key'] == 'SYNTHETIC_POOL_SECONDARY'
    assert again['source'] == 'pool:paid'
    assert again['requested_provider'] == runtime['requested_provider'] == 'custom:paid'
    assert (again['provider'], again['base_url'], again['api_mode']) == (
        runtime['provider'], runtime['base_url'], runtime['api_mode'])