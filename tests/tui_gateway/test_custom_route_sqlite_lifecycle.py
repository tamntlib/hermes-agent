"""Real SQLite lifecycle: default first submit -> build -> persisted route -> resume."""
import json
from types import SimpleNamespace
import pytest
from hermes_state import SessionDB
from tui_gateway import server
from hermes_cli import runtime_provider as rp


@pytest.mark.parametrize('tier',['priority',None])
def test_default_creation_attachment_and_resume_use_same_named_wire(tmp_path,monkeypatch,tier):
    model='gpt-6.1-sol'
    url='https://work-route.invalid/v1'
    cfg={'model':{'default':model,'provider':'responses'},'providers':{
        'responses':{'base_url':url,'api_mode':'codex_responses','api_key':'test-key','service_tiers':['priority'],'models':{model:{}}},
        'messages':{'base_url':'https://work-route.invalid','api_mode':'anthropic_messages','api_key':'test-key','models':{model:{}}}}}
    monkeypatch.setattr(rp,'load_config',lambda:cfg)
    monkeypatch.setattr(rp,'_get_model_config',lambda:cfg['model'])
    monkeypatch.setattr(rp,'_try_resolve_from_custom_pool',lambda *a,**k:None)
    monkeypatch.setattr(server,'_session_default_route',lambda _:(model,{}))
    monkeypatch.setattr(server,'_register_session_cwd',lambda _:None)
    monkeypatch.setattr(server,'_session_todo_state',lambda _:None)
    monkeypatch.setattr(server,'_config_model_target',lambda:(model,'responses'))
    monkeypatch.setattr('socket.socket.connect',lambda *a,**k:pytest.fail('network forbidden'))
    sid='20261022_000000_abcd12'
    rec={'session_key':sid,'model_override':None,'create_service_tier_override':tier}
    monkeypatch.setattr(server,'_sessions',{'ui':rec})
    db=SessionDB(tmp_path/'state.db')
    try:
        selected,initial=server._workdir_row_model_config(rec)
        db.create_session(sid,source='desktop',model=selected,model_config=initial)
        running=SimpleNamespace(model=model,provider='custom',requested_provider='custom:responses',
            base_url=url,api_mode='codex_responses',service_tier=tier,reasoning_config=None,_session_db=db)
        assert server._attach_built_agent('ui',rec,running) is True
        row=db.get_session(sid)
        stored=json.loads(row['model_config'])
        assert stored['provider']=='custom:responses'
        assert stored['base_url']==url
        assert stored['api_mode']=='codex_responses'
        assert 'api_key' not in stored
        overrides=server._stored_session_runtime_overrides(row)
        selected,runtime=server._resolve_agent_model_runtime(overrides['model_override'],overrides['provider_override'])
        assert selected==model
        assert runtime['api_mode']=='codex_responses'
        assert runtime['base_url']==url
        assert runtime['requested_provider']=='custom:responses'
        assert overrides.get('service_tier_override')==tier
    finally:
        db.close()


@pytest.fixture
def resumed_normal_session(tmp_path, monkeypatch):
    model = 'gpt-6.1-sol'
    url = 'https://work-route.invalid/v1'
    cfg = {'model': {'default': model, 'provider': 'responses'},
           'agent': {'service_tier': 'priority'}, 'providers': {
               'responses': {'base_url': url, 'api_mode': 'codex_responses',
                             'api_key': 'test-key', 'service_tiers': ['priority'],
                             'models': {model: {}}}}}
    monkeypatch.setattr(rp, 'load_config', lambda: cfg)
    monkeypatch.setattr(rp, '_get_model_config', lambda: cfg['model'])
    monkeypatch.setattr(rp, '_try_resolve_from_custom_pool', lambda *a, **k: None)
    monkeypatch.setattr(server, '_load_cfg', lambda: cfg)
    monkeypatch.setattr(server, '_register_session_cwd', lambda _: None)
    monkeypatch.setattr(server, '_session_todo_state', lambda _: None)
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr('socket.socket.connect', lambda *a, **k: pytest.fail('network forbidden'))
    sid = '20261022_000001_abcd12'
    db = SessionDB(tmp_path / 'state.db')

    def resume(row):
        overrides = server._stored_session_runtime_overrides(row)
        ctx = server._Resume(1, {}, sid)
        ctx.found = row
        rec = ctx.record('desktop', str(tmp_path), [], overrides)
        assert 'create_service_tier_override' not in rec
        assert rec['resume_runtime_overrides']['service_tier_override'] == ''
        kwargs = server._deferred_build_agent_kwargs(rec, db)
        assert kwargs['service_tier_override'] == ''
        selected, runtime = server._resolve_agent_model_runtime(
            kwargs['model_override'], kwargs['provider_override'])
        assert runtime['requested_provider'] == 'custom:responses'
        assert runtime['api_mode'] == 'codex_responses'
        # Match _make_agent's override-vs-profile choice and AIAgent's normal normalization.
        from agent.fast_mode import parse_service_tier
        tier = kwargs.get('service_tier_override')
        tier = server._load_service_tier() if tier is None else tier
        running = SimpleNamespace(model=selected, provider=runtime['provider'],
                                  requested_provider=runtime['requested_provider'],
                                  base_url=runtime['base_url'], api_mode=runtime['api_mode'],
                                  service_tier=parse_service_tier(tier),
                                  reasoning_config=None, _session_db=db)
        assert running.service_tier is None
        server._sessions['ui'] = rec
        assert server._attach_built_agent('ui', rec, running) is True
        return rec, running

    try:
        yield db, sid, model, url, resume
    finally:
        db.close()


@pytest.mark.parametrize('stored_provider,stored_tier', [
    ('custom:responses', 'normal'),
    ('custom', 'priority'),
], ids=['named-normal', 'legacy-inferred-normal'])
def test_repeated_resume_preserves_normal_under_priority_profile(
        resumed_normal_session, stored_provider, stored_tier):
    db, sid, model, url, resume = resumed_normal_session
    db.create_session(sid, source='desktop', model=model, model_config={
        'provider': stored_provider, 'base_url': url, 'api_mode': 'codex_responses',
        'service_tier': stored_tier, 'unrelated': {'keep': True}})
    assert server._load_service_tier() == 'priority'
    for _ in range(2):
        resume(db.get_session(sid))
        stored = json.loads(db.get_session(sid)['model_config'])
        assert stored.get('service_tier') == 'normal'
        assert stored['provider'] == 'custom:responses'
        assert stored['unrelated'] == {'keep': True}
        assert server._stored_session_runtime_overrides(db.get_session(sid))['service_tier_override'] == ''


def test_live_priority_switch_wins_over_stale_resume_normal(resumed_normal_session):
    db, sid, model, url, resume = resumed_normal_session
    db.create_session(sid, source='desktop', model=model, model_config={
        'provider': 'custom:responses', 'base_url': url,
        'api_mode': 'codex_responses', 'service_tier': 'normal'})
    rec, running = resume(db.get_session(sid))
    running.service_tier = 'priority'
    assert rec['resume_runtime_overrides']['service_tier_override'] == ''
    server._persist_live_session_runtime(rec)
    stored = json.loads(db.get_session(sid)['model_config'])
    assert stored['service_tier'] == 'priority'
    assert server._stored_session_runtime_overrides(db.get_session(sid))['service_tier_override'] == 'priority'
