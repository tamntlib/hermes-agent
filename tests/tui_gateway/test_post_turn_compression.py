"""The reply is visible before post-turn compression; follow-ups reuse the busy queue."""
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tests.tui_gateway import test_auto_continue

turn_env = test_auto_continue.turn_env
marker_home = test_auto_continue.marker_home
_session = test_auto_continue._session


def test_completed_turn_delivers_then_compresses_before_release(turn_env, marker_home, monkeypatch):
    import agent.post_turn_compression as policy

    events = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.append(event))
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    monkeypatch.setattr(policy, "post_turn_compression_due", lambda *a, **k: True)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"compression": {"threshold_buffer": 0.02}})
    agent = SimpleNamespace(clear_interrupt=lambda: None)
    session = _session(agent=agent, running=True)
    agent.run_conversation = lambda *a, **k: {"final_response": "answer", "messages": [
        {"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]}

    def compress(current, **kwargs):
        assert "message.complete" in events
        assert current["running"] is True
        assert server._session_compression_in_flight(current) is True
        reply = server._handle_busy_submit("next", "sid", current, "follow-up", "transport")
        assert reply["result"]["status"] == "queued"
        assert kwargs["automatic"] is True
        events.append("compress")
        return 0, {}

    monkeypatch.setattr(server, "_compress_session_history", compress)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a, **k: events.append("drain"))
    server._run_prompt_submit("request", "sid", session, "question")
    assert events.index("message.complete") < events.index("compress") < events.index("drain")
    assert session["running"] is False
    assert not session.get("_manual_compress_active")
    assert session["queued_prompt"]["text"] == "follow-up"


@pytest.mark.parametrize("in_place", [False, True])
def test_post_reply_reuses_database_compression_without_duplicate_persist(tmp_path, monkeypatch, in_place):
    from tests.tui_gateway.test_manual_compress_history_fence import _agent_with_history
    from agent.conversation_compression import finalize_context_engine_compression_notification

    db, agent, parent, before = _agent_with_history(tmp_path, monkeypatch, in_place)
    session = _session(agent=agent, session_key=parent, history=before, history_version=1, running=True,
                       cwd=str(tmp_path))
    agent._session_messages = before
    try:
        removed, _ = server._compress_session_history(session, automatic=True)
        finalize_context_engine_compression_notification(agent, committed=True)
        assert removed > 0
        assert agent._session_messages is session["history"]
        assert agent.context_compressor.compress.call_args.kwargs["force"] is False
        active = db.get_messages_as_conversation(agent.session_id, include_row_ids=True)
        assert [row["content"] for row in active] == [row["content"] for row in session["history"]]
        assert len({row["_row_id"] for row in active}) == len(active)
        assert (agent.session_id == parent) is in_place
        # A normal later flush must not insert the compacted rows again.
        agent._persist_session(session["history"], None)
        assert [row["content"] for row in db.get_messages_as_conversation(agent.session_id)] == [row["content"] for row in active]
    finally:
        agent.close()
        db.close()


def test_automatic_cooldown_noop_does_not_rewrite_host_history(tmp_path, monkeypatch):
    from tests.tui_gateway.test_manual_compress_history_fence import _agent_with_history
    db, agent, parent, before = _agent_with_history(tmp_path, monkeypatch)
    session = _session(agent=agent, session_key=parent, history=before, history_version=1, running=True)
    monkeypatch.setattr(agent, "_compress_context", lambda messages, *a, **k: (messages, "prompt"))
    try:
        removed, _ = server._compress_session_history(session, automatic=True)
        assert removed == 0
        assert session["history"] is before
        assert session["history_version"] == 1
    finally:
        agent.close()
        db.close()


@pytest.mark.parametrize("status", ["error", "interrupted"])
def test_unsuccessful_turn_never_runs_post_reply_compression(monkeypatch, status):
    import agent.post_turn_compression as policy
    monkeypatch.setattr(policy, "post_turn_compression_due", lambda *a, **k: pytest.fail("must not check"))
    frames = []
    monkeypatch.setattr(server, "_emit", lambda *args: frames.append(args))
    session = _session(running=True)
    payload = {"status": status, "text": "partial"}
    server._emit_complete_and_compress("sid", session, payload)
    assert frames == [("message.complete", "sid", payload)]
    assert not session.get("_manual_compress_active")


def test_compression_failure_cannot_change_the_delivered_answer(monkeypatch):
    import agent.post_turn_compression as policy
    monkeypatch.setattr(policy, "post_turn_compression_due", lambda *a, **k: True)
    monkeypatch.setattr(server, "_load_cfg", dict)
    session = _session(running=True)
    frames = []
    def emit(event, sid, payload=None):
        if event == "message.complete":
            assert server._session_compression_in_flight(session) is True
        frames.append(event)
    monkeypatch.setattr(server, "_emit", emit)
    def fail(*a, **k):
        raise RuntimeError("fixture summarizer unavailable")
    monkeypatch.setattr(server, "_compress_session_history", fail)
    payload = {"status": "complete", "text": "answer"}
    server._emit_complete_and_compress("sid", session, payload)
    assert frames == ["message.complete"]
    assert payload == {"status": "complete", "text": "answer"}
    assert not session.get("_manual_compress_active")
