"""Real resume-helper regressions for paid Responses + same-model Messages sibling."""
from tests.tui_gateway.conftest import (
    _join_leaked_turn_workers,
)

import copy
import json
from unittest.mock import patch

import pytest
from hermes_cli import runtime_provider as rp
from tui_gateway import server

MODEL='gpt-6.1-sol'
URL='https://work-route.invalid/v1'
CONFIG={'model':{'provider':'responses','default':MODEL},'providers':{
    'responses':{'base_url':URL,'api_mode':'codex_responses','api_key':'test-key','fast_mode':True,'models':{MODEL:{}}},
    'messages':{'base_url':'https://work-route.invalid','api_mode':'anthropic_messages','api_key':'test-key','models':{MODEL:{}}},
}}


@pytest.fixture
def config(monkeypatch):
    value=copy.deepcopy(CONFIG)
    monkeypatch.setattr(rp,'load_config',lambda:value)
    monkeypatch.setattr(rp,'_get_model_config',lambda:value['model'])
    monkeypatch.setattr(rp,'_try_resolve_from_custom_pool',lambda *a,**k:None)
    monkeypatch.setattr('socket.socket.connect',lambda *a,**k:pytest.fail('network forbidden'))
    return value


@pytest.mark.parametrize('preference',['normal','priority'])
def test_bare_billing_route_is_restored_as_responses_without_minting_paid_authority(config,preference):
    row={'model':MODEL,'billing_provider':'custom','billing_base_url':URL,
         'model_config':json.dumps({'service_tier':preference})}
    overrides=server._stored_session_runtime_overrides(row)
    assert overrides['model_override']['provider']=='custom:responses'
    assert overrides['model_override']['base_url']==URL
    assert overrides['service_tier_override']==('' if preference == 'normal' else 'priority')
    model,runtime=server._resolve_agent_model_runtime(overrides['model_override'],overrides['provider_override'])
    assert model==MODEL
    assert runtime['api_mode']=='codex_responses'
    assert runtime['base_url']==URL
    assert runtime['requested_provider']=='custom:responses'


def test_explicit_named_identity_retains_explicit_priority(config):
    row={'model':MODEL,'billing_provider':'custom','billing_base_url':URL,
         'model_config':json.dumps({'provider':'custom:responses','base_url':URL,'api_mode':'codex_responses','service_tier':'priority'})}
    overrides=server._stored_session_runtime_overrides(row)
    assert overrides['provider_override']=='custom:responses'
    assert overrides['service_tier_override']=='priority'


@pytest.mark.parametrize('mode', ['', 'codex_responses'])
def test_unknown_exact_endpoint_does_not_guess_same_model_sibling(config, mode):
    row={'model':MODEL,'billing_provider':'custom','billing_base_url':'https://unknown.invalid/v1',
         'model_config':json.dumps({'api_mode': mode})}
    with pytest.raises(ValueError,match='(?i)(restore|route|provider)'):
        server._stored_session_runtime_overrides(row)
