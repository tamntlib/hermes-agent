"""Regression tests for durable route capture at real runtime attachment/first activity."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tui_gateway import server

MODEL='gpt-6.1-sol'
URL='https://work-route.invalid/v1'
PROVIDER='custom:responses'


@pytest.fixture(autouse=True)
def configured_route_owner(monkeypatch):
    monkeypatch.setattr('hermes_cli.runtime_provider.load_config', lambda: {'providers': {
        'responses': {'base_url': URL, 'api_mode': 'codex_responses', 'service_tiers': ['priority']},
    }})


def agent(db=None):
    return SimpleNamespace(model=MODEL,provider='custom',requested_provider=PROVIDER,
        base_url=URL,api_mode='codex_responses',service_tier='priority',reasoning_config={'effort':'high'},_session_db=db)


class DB:
    def __init__(self):
        self.row={'model':MODEL,'model_config':json.dumps({'reasoning_config':{'effort':'high'},'service_tier':'priority','private_setting':'preserve'})}
        self.writes=0
    def get_session(self,key):
        return self.row
    def update_session_meta(self,key,config,model):
        self.writes+=1
        self.row.update(model_config=config,model=model)


def test_attachment_repairs_existing_first_submit_row_with_actual_named_route(monkeypatch):
    db=DB()
    rec={'session_key':'session-key','model_override':None}
    monkeypatch.setattr(server,'_sessions',{'ui':rec})
    monkeypatch.setattr(server,'_register_session_cwd',lambda _:None)
    monkeypatch.setattr(server,'_session_todo_state',lambda _:None)
    monkeypatch.setattr(server,'_config_model_target',lambda:(MODEL,PROVIDER))
    assert server._attach_built_agent('ui',rec,agent(db)) is True
    config=json.loads(db.row['model_config'])
    assert config.get('provider')==PROVIDER
    assert config.get('base_url')==URL
    assert config.get('api_mode')=='codex_responses'
    assert config.get('service_tier')=='priority'
    assert config['private_setting']=='preserve'
    assert db.writes==1
    assert 'api_key' not in config


def test_first_activity_with_prebuilt_agent_persists_its_actual_route(monkeypatch):
    monkeypatch.setattr(server,'_session_default_route',lambda _:(MODEL,{}))
    rec={'session_key':'key','agent':agent(),'model_override':None,'create_service_tier_override':'priority'}
    model,config=server._workdir_row_model_config(rec)
    assert model==MODEL
    assert config.get('provider')==PROVIDER
    assert config.get('base_url')==URL
    assert config.get('api_mode')=='codex_responses'
    assert config.get('service_tier')=='priority'
    assert 'api_key' not in config


def test_replaced_record_never_persists_or_attaches(monkeypatch):
    rec={'session_key':'old'}
    monkeypatch.setattr(server,'_sessions',{'ui':{'session_key':'new'}})
    persist=Mock()
    monkeypatch.setattr(server,'_persist_live_session_runtime',persist)
    assert server._attach_built_agent('ui',rec,agent()) is False
    assert 'agent' not in rec
    persist.assert_not_called()
