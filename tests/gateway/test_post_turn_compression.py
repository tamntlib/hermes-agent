"""Canonical post-reply compaction uses the existing fenced mutation, in the FIFO drain."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
import hermes_bootstrap  # Activate payload dependencies before the per-test home I/O guard.

from gateway.session_authority import SessionAuthority
from gateway.session_contract import Submission
from gateway.session_controls import AuthorityConnection
from gateway.session_local import create_local_session
from tests.gateway.test_compression_runtime_authority import DENIED_DESTINATIONS, FALLBACK_DESTINATIONS
import hermes_state_runtime as rt


def _local(tmp_path, monkeypatch, *, engine='compressor', source='cli', seed_turns=4, **compression):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'model': {'default': 'frozen', 'context_length': 100000}, 'context': {'engine': engine},
              'compression': {'threshold': .90, 'threshold_tokens': None,
                              'threshold_buffer': .02, **compression}}
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a, **k: config)
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                             _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='owner', db=db, epoch=epoch)
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    ref = create_local_session(authority, owner.actor, dict(request_id='create', source=source, cwd=str(tmp_path),
                                                          model='frozen', toolsets=[]))
    for i in range(seed_turns):
        db.append_message(ref.session_id, 'user', f'question {i}')
        db.append_message(ref.session_id, 'assistant', f'answer {i}')
    return authority, owner, ref


@pytest.mark.asyncio
@pytest.mark.parametrize('summary_failure', [False, True], ids=['committed', 'failure-keeps-reply'])
async def test_delivered_reply_precedes_compression_and_queued_turn_waits(tmp_path, monkeypatch, summary_failure):
    from agent.context_compressor import ContextCompressor
    from agent.usage_anchor import capture_usage_anchor
    from gateway import session_finite

    authority, owner, ref = _local(tmp_path, monkeypatch)
    db = authority.db
    live = authority.sessions[ref.session_id]
    frames, starts, compress_calls, compactions = [], [], [], []
    monkeypatch.setattr('agent.compaction_events.fire_post_compaction', lambda **kw: compactions.append(kw))
    live.event_stream.observers.add(frames.append)
    executing, finish = asyncio.Event(), asyncio.Event()
    summarizing, release = threading.Event(), threading.Event()

    async def execute(authority, ref, row):
        starts.append(row['request_id'])
        if row['request_id'] == 'first':
            executing.set()
            await finish.wait()
        history = db.get_messages_as_conversation(ref.session_id)
        user = {'role': 'user', 'content': row['payload']['text']}
        answer = {'role': 'assistant', 'content': 'reply ' + row['request_id']}
        db.append_message(ref.session_id, 'user', user['content'])
        db.append_message(ref.session_id, 'assistant', answer['content'])
        # Real provider usage anchored to the persisted prefix: tiny text still crosses .88.
        anchor = capture_usage_anchor(88000 if row['request_id'] == 'first' else 100, 1, history + [user])
        db.patch_session_model_config(ref.session_id, {'_usage_anchor': anchor})
        authority.pending_results[row['admission_id']] = {'result': {
            'final_response': answer['content'], 'messages': history + [user, answer]}, 'usage': {}}
        return answer['content']

    def compress(self, messages, **kwargs):
        compress_calls.append(kwargs)
        assert any(f['params']['type'] == 'message.complete' for f in frames)
        summarizing.set()
        assert release.wait(5), 'test did not release compression'
        if summary_failure:
            raise RuntimeError('summary failed')
        return [{'role': 'user', 'content': 'summary', '_compressed_summary': True}, *messages[-2:]]

    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    monkeypatch.setattr(ContextCompressor, 'compress', compress)
    try:
        first = await authority.submit(owner.actor, Submission('first', ref, {'text': 'first'}, 'queue'))
        waiter = authority.waiters[first.admission_id] = asyncio.get_running_loop().create_future()
        await asyncio.wait_for(executing.wait(), 2)
        await authority.submit(owner.actor, Submission('second', ref, {'text': 'second'}, 'queue'))
        finish.set()
        assert await asyncio.to_thread(summarizing.wait, 2), 'completed reply never triggered compression'
        assert starts == ['first'], 'queued turn ran before post-reply compression finished'
        assert waiter.done() and waiter.result() == 'reply first', 'reply waiter was held behind compression'
        complete = [f for f in frames if f['params']['type'] == 'message.complete']
        assert len(complete) == 1 and complete[0]['params']['payload']['text'] == 'reply first'
        assert compress_calls[0]['force'] is False, 'automatic compression must preserve automatic guards'
        assert compress_calls[0].get('current_tokens') == 88001
        release.set()
        await asyncio.wait_for(live.task, 5)
        assert starts == ['first', 'second']
        messages = db.get_messages_as_conversation(ref.session_id)
        expected = (['summary'] if not summary_failure else
                    [value for i in range(4) for value in (f'question {i}', f'answer {i}')])
        assert [m['content'] for m in messages] == expected + ['first', 'reply first', 'second', 'reply second']
        assert len([f for f in frames if f['params']['type'] == 'message.complete']) == 2
        assert any(f['params']['type'] == 'session.updated' for f in frames) is not summary_failure
        assert bool(db.list_meta_prefix('gateway.mutation.v1.')) is not summary_failure
        assert [event['reason'] for event in compactions] == ([] if summary_failure else ['between_turns'])
    finally:
        finish.set()
        release.set()
        if live.task is not None:
            await asyncio.wait_for(live.task, 5)
        await owner.close()


@pytest.mark.asyncio
async def test_external_context_engine_is_not_replaced_by_builtin_compression(tmp_path, monkeypatch):
    from agent.context_compressor import ContextCompressor
    from agent.usage_anchor import capture_usage_anchor
    from gateway.session_post_turn_compression import compress_after_reply

    authority, owner, ref = _local(tmp_path, monkeypatch, engine='external')
    db = authority.db
    messages = db.get_messages_as_conversation(ref.session_id)
    db.patch_session_model_config(ref.session_id, {'_usage_anchor': capture_usage_anchor(88001, 0, messages)})
    calls = []
    monkeypatch.setattr(ContextCompressor, 'compress', lambda self, messages, **kw: calls.append(kw) or messages)
    row = db.get_session(ref.session_id)
    try:
        await compress_after_reply(authority, ref, authority.sessions[ref.session_id], {
            'admission_id': 'delivered', 'principal_id': owner.actor.subject,
            'generation': row['runtime_generation'], 'payload': {}})
        assert calls == [], 'post-reply hook bypassed the external engine policy'
        assert not db.list_meta_prefix('gateway.mutation.v1.')
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['small', 'below-trigger', 'disabled', 'native', 'checkpoint', 'finite', 'unattended', 'automation', 'guarded'])
async def test_automatic_ineligible_paths_are_read_only(tmp_path, monkeypatch, case):
    from agent.context_compressor import ContextCompressor
    from agent.usage_anchor import capture_usage_anchor
    from gateway.session_post_turn_compression import compress_after_reply

    options: dict = {'threshold_buffer': None} if case == 'disabled' else {}
    if case == 'native':
        options['codex_responses_native'] = True
    if case == 'checkpoint':
        options['checkpoint_required'] = True
    authority, owner, ref = _local(tmp_path, monkeypatch, seed_turns=1 if case == 'small' else 4, **options)
    db = authority.db
    messages = db.get_messages_as_conversation(ref.session_id)
    tokens = 87999 if case == 'below-trigger' else 88001
    db.patch_session_model_config(ref.session_id, {'_usage_anchor': capture_usage_anchor(tokens, 0, messages)})
    calls = []
    monkeypatch.setattr(ContextCompressor, 'compress', lambda self, messages, **kw: calls.append(kw) or messages)
    if case == 'guarded':
        monkeypatch.setattr(ContextCompressor, '_automatic_compression_blocked', lambda self: True)
    row = db.get_session(ref.session_id)
    payload: dict = {case: True} if case in ('finite', 'unattended') else {}
    if case == 'automation':
        payload['local_automation_v1'] = {}
    try:
        await compress_after_reply(authority, ref, authority.sessions[ref.session_id], {
            'admission_id': 'delivered', 'principal_id': owner.actor.subject,
            'generation': row['runtime_generation'], 'payload': payload})
        assert calls == [], case
        after = db.get_session(ref.session_id)
        assert after['runtime_revision'] == row['runtime_revision']
        assert after['runtime_generation'] == row['runtime_generation']
        assert not db.list_meta_prefix('gateway.mutation.v1.')
        assert db.get_messages_as_conversation(ref.session_id) == messages
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('in_place', [False, True])
@pytest.mark.parametrize('aborted', [False, True], ids=['structural-noop', 'summary-aborted'])
async def test_existing_summary_does_not_turn_an_unchanged_result_into_a_commit(
    tmp_path, monkeypatch, in_place, aborted,
):
    from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX
    from agent.usage_anchor import capture_usage_anchor
    from gateway.session_post_turn_compression import compress_after_reply

    authority, owner, ref = _local(tmp_path, monkeypatch, in_place=in_place)
    db = authority.db
    # Preserve the stored summary flag at the read boundary, as the DB's live
    # replay/row-id read variants do. The result must not use an OLD flag as
    # proof that the current attempt compressed anything.
    before = db.get_messages_as_conversation(ref.session_id)
    db.replace_messages(ref.session_id, [{'role': 'user', 'content': SUMMARY_PREFIX + ' earlier work',
                                         '_compressed_summary': True}, *before])
    load = db.get_messages_as_conversation
    monkeypatch.setattr(db, 'get_messages_as_conversation',
                        lambda sid: load(sid, repair_alternation=True))
    messages = db.get_messages_as_conversation(ref.session_id)
    assert any(m.get('_compressed_summary') for m in messages)
    db.patch_session_model_config(ref.session_id, {'_usage_anchor': capture_usage_anchor(88001, 0, messages)})
    calls, events = [], []
    monkeypatch.setattr('agent.compaction_events.fire_post_compaction', lambda **kw: events.append(kw))
    def unchanged(self, current, **kwargs):
        calls.append(kwargs)
        self._last_compress_aborted = aborted
        return current
    monkeypatch.setattr(ContextCompressor, 'compress', unchanged)
    row = db.get_session(ref.session_id)
    try:
        await compress_after_reply(authority, ref, authority.sessions[ref.session_id], {
            'admission_id': 'delivered', 'principal_id': owner.actor.subject,
            'generation': row['runtime_generation'], 'payload': {}})
        assert len(calls) == 1
        after = db.get_session(ref.session_id)
        assert after['runtime_revision'] == row['runtime_revision']
        assert after['runtime_generation'] == row['runtime_generation']
        assert [m['content'] for m in load(ref.session_id)] == [SUMMARY_PREFIX + ' earlier work', *[m['content'] for m in before]]
        assert not db.list_meta_prefix('gateway.mutation.v1.')
        assert events == []
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,url,expected', FALLBACK_DESTINATIONS)
async def test_post_reply_fallback_rechecks_actual_destination(tmp_path, monkeypatch, provider, url, expected):
    from tests.gateway.test_compression_runtime_authority import fallback_session

    authority, owner, ref, first, second = fallback_session(tmp_path, monkeypatch, provider, url)
    try:
        await canonical_post_reply(authority, owner, ref)
        assert first.requests and second.requests
        assert all(r.get('extra_body', {}).get('service_tier') == 'priority' for r in first.requests)
        assert all(r.get('extra_body', {}).get('service_tier') == expected for r in second.requests)
    finally:
        await owner.close()


async def canonical_post_reply(authority, owner, ref):
    from agent.usage_anchor import capture_usage_anchor
    from gateway.session_post_turn_compression import compress_after_reply

    messages = authority.db.get_messages_as_conversation(ref.session_id)
    authority.db.patch_session_model_config(ref.session_id, {'_usage_anchor': capture_usage_anchor(8801, 0, messages)})
    row = authority.db.get_session(ref.session_id)
    await compress_after_reply(authority, ref, authority.sessions[ref.session_id], {
        'admission_id': 'tier-contract', 'principal_id': owner.actor.subject,
        'generation': row['runtime_generation'], 'payload': {}})


@pytest.mark.asyncio
@pytest.mark.parametrize('pooled', [True, False], ids=['real-pool', 'explicit-launch-key'])
@pytest.mark.parametrize('selected', ['custom:paid', 'main', 'auto'])
async def test_post_reply_compression_preserves_selected_named_tier(tmp_path, monkeypatch, selected, pooled):
    from tests.gateway.test_compression_runtime_authority import canonical_session

    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch, selected=selected, pooled=pooled)
    try:
        await canonical_post_reply(authority, owner, ref)
        assert client.requests, 'canonical compressor never dispatched a summary'
        assert all(r.get('extra_body', {}).get('service_tier') == 'priority' for r in client.requests)
        assert len(authority.db.get_messages_as_conversation(ref.session_id)) < 24
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('pooled', [True, False], ids=['real-pool', 'explicit-launch-key'])
@pytest.mark.parametrize('selected,main,destination', DENIED_DESTINATIONS)
async def test_post_reply_compression_cannot_borrow_foreign_or_sibling_tier(
    tmp_path, monkeypatch, selected, main, destination, pooled,
):
    from agent import auxiliary_client as aux
    from tests.gateway.test_compression_runtime_authority import canonical_session, foreign_runtime

    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch,
        selected=selected, main=main, destination=destination, pooled=pooled)
    token = foreign_runtime()
    prior = aux._RUNTIME_MAIN_CONTEXT.get()
    try:
        await canonical_post_reply(authority, owner, ref)
        assert client.requests, 'denial must exercise an actual assembled send'
        assert all('service_tier' not in r.get('extra_body', {}) for r in client.requests)
        assert aux._RUNTIME_MAIN_CONTEXT.get() is prior
    finally:
        aux.reset_runtime_main(token)
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', [False, True], ids=['no-prior', 'foreign-prior'])
@pytest.mark.parametrize('fail', [False, True], ids=['success', 'summary-exception'])
async def test_post_reply_summary_runtime_is_owned_then_restored(tmp_path, monkeypatch, nested, fail):
    from agent import auxiliary_client as aux
    from tests.gateway.test_compression_runtime_authority import (
        canonical_session, foreign_runtime, observe_summary_runtime)

    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch)
    before = authority.db.get_session(ref.session_id)
    messages = authority.db.get_messages_as_conversation(ref.session_id)
    token = foreign_runtime() if nested else aux._RUNTIME_MAIN_CONTEXT.set(None)
    prior = aux._RUNTIME_MAIN_CONTEXT.get()
    restorations, scopes = observe_summary_runtime(monkeypatch, fail=fail)
    try:
        await canonical_post_reply(authority, owner, ref)
        assert client.requests, 'exception control must still reach actual summary dispatch'
        assert all(r.get('extra_body', {}).get('service_tier') == 'priority' for r in client.requests)
        assert scopes and all(s == (ref.session_id, ref.session_id, 'custom:paid') for s in scopes)
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
