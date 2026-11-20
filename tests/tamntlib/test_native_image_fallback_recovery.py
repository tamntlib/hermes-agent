"""Native artifacts survive real route fallback into length-recovery without bypassing transforms."""
from tests.agent.conftest import (
    _fast_retry_backoff,
    _fresh_structured_output_memo,
)

import json
from types import SimpleNamespace as NS

import pytest

from hermes_state import SessionDB
from tests.tamntlib.test_native_responses_images import image_item, media_paths
from tests.tamntlib.responses_test_support import response

pytest_plugins = ("tests.tamntlib.responses_test_support",)


@pytest.mark.parametrize("refused_response", [False, True], ids=["transport-error", "same-reply-refusal"])
def test_native_image_fallback_length_final_is_transformed_before_flush(make_agent, monkeypatch, tmp_path, refused_response):
    url = "https://fallback.invalid/v1"
    (tmp_path / "config.yaml").write_text(json.dumps({"providers": {"fallback": {
        "base_url": url, "api_mode": "chat_completions", "api_key": "offline-fixture"}}}))
    agent = make_agent(native=True, tools=True)
    agent.max_iterations = 12
    agent._fallback_chain = [{"provider": "custom:fallback", "model": "private-model",
                              "base_url": url, "api_mode": "chat_completions", "api_key": "offline-fixture"}]
    agent._session_db = SessionDB(tmp_path / "fallback.db")
    agent._ensure_db_session()
    monkeypatch.setattr("agent.auxiliary_client._create_openai_client",
                        lambda **kw: NS(base_url=url, api_key="offline-fixture", _custom_headers={}))
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **kw: '{"ok": true}')
    seen, calls, final_flushes = [], [], []

    def invoke(name, **payload):
        if name == "transform_llm_output":
            seen.append(payload["response_text"])
            return ["Reviewed fallback partial."]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    original = agent._persist_session

    def persist(messages, history):
        if any(row.get("finish_reason") == "length" and not row.get("_length_continuation_fragment")
               for row in messages):
            final_flushes.append((len(seen), messages[-1]["content"]))
        return original(messages, history)

    monkeypatch.setattr(agent, "_persist_session", persist)
    first = response(image_item(), NS(type="function_call", id="fc", call_id="call", name="terminal",
                                      status="completed", arguments="{}"))

    refusal = response(image_item(), status="incomplete", response_id="refused")
    refusal.incomplete_details = NS(reason="content_filter")
    native_calls = 1 if refused_response else 2

    def send(kwargs, **callbacks):
        calls.append((agent.api_mode, kwargs))
        assert len(calls) <= native_calls + 4
        if len(calls) == 1:
            return refusal if refused_response else first
        if not refused_response and len(calls) == 2:
            error = RuntimeError("402 synthetic insufficient credits")
            error.status_code = 402
            raise error
        return NS(id="partial", choices=[NS(message=NS(content="A partial answer.", tool_calls=[]),
                                             finish_reason="length")],
                  usage=NS(prompt_tokens=17, completion_tokens=3, total_tokens=20))

    monkeypatch.setattr(agent, "_interruptible_api_call", send)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", send)
    try:
        result = agent.run_conversation("Produce an image and explain it.")
        assert len(calls) == native_calls + 4
        assert [mode for mode, _ in calls] == ["codex_responses"] * native_calls + ["chat_completions"] * 4
        if refused_response:
            assert "MEDIA:" not in json.dumps(calls[native_calls][1]["messages"])
        assert len(seen) == 1
        assert len(media_paths(seen[0])) == 1 and media_paths(seen[0])[0].is_file()
        assert result["final_response"] == "Reviewed fallback partial."
        assert result["response_transformed"] is True
        assert result["pre_transform_response"] == seen[0]
        assert final_flushes and all(count == 1 and text == result["final_response"] for count, text in final_flushes)
        assert agent._session_db.get_messages_as_conversation(agent.session_id)[-1]["content"] == result["final_response"]
    finally:
        agent._session_db.close()


@pytest.mark.parametrize("with_image", [False, True], ids=["text", "image"])
@pytest.mark.parametrize("withhold", [False, True], ids=["rewrite", "withhold"])
def test_held_http_refusal_owns_final_transform_after_continuation(make_agent, monkeypatch, tmp_path, with_image, withhold):
    from agent.turn_finalizer import HELD_TRANSFORM_FAILED_REPLY
    from tests.tamntlib.responses_test_support import text_item

    url = "https://fallback.invalid/v1"
    (tmp_path / "config.yaml").write_text(json.dumps({"providers": {"fallback": {
        "base_url": url, "api_mode": "chat_completions", "api_key": "offline-fixture"}}}))
    agent = make_agent(native=True)
    agent.max_iterations = 8
    agent._fallback_chain = [{"provider": "custom:fallback", "model": "private-model",
                              "base_url": url, "api_mode": "chat_completions", "api_key": "offline-fixture"}]
    monkeypatch.setattr("agent.auxiliary_client._create_openai_client",
                        lambda **kw: NS(base_url=url, api_key="offline-fixture", _custom_headers={}))
    raw = response(image_item() if with_image else text_item("Primary refusal detail."), status="incomplete")
    raw.incomplete_details = NS(reason="content_filter")
    fragment, detail = "Discarded continuation text.", "Content Exists Risk: fixture provider detail"
    calls, seen, streamed = [], [], []

    def rewrite(**payload):
        seen.append(payload["response_text"])
        if withhold:
            raise RuntimeError("offline transform failure")
        return "Reviewed terminal refusal."

    def send(kwargs, **callbacks):
        calls.append(agent.api_mode)
        if len(calls) == 1:
            return raw
        if len(calls) == 2:
            assert "MEDIA:" not in json.dumps(kwargs["messages"])
            return NS(id="partial", choices=[NS(message=NS(content=fragment, tool_calls=[]), finish_reason="length")],
                      usage=NS(prompt_tokens=17, completion_tokens=3, total_tokens=20))
        assert len(calls) == 3 and fragment in json.dumps(kwargs["messages"])
        error = RuntimeError(detail)
        error.status_code = 400
        raise error

    monkeypatch.setattr("agent.conversation_loop.held_stream_transforms", lambda: (rewrite,))
    monkeypatch.setattr(agent, "_interruptible_api_call", send)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", send)
    agent._session_db = SessionDB(tmp_path / "held-refusal.db")
    agent._ensure_db_session()
    db = agent._session_db
    try:
        db.append_message(agent.session_id, "user", "Accepted prompt")
        db.append_message(agent.session_id, "assistant", "Accepted reply")
        history = db.get_messages_as_conversation(agent.session_id, include_row_ids=True)
        result = agent.run_conversation("Refused prompt", conversation_history=history, stream_callback=streamed.append)
        assert calls == ["codex_responses", "chat_completions", "chat_completions"]
        assert (result["failure_reason"], result["failure_retryable"]) == ("content_policy_blocked", False)
        assert result["failed"] and not result["completed"]
        assert len(seen) == 1 and detail in seen[0] and fragment not in seen[0]
        assert len(media_paths(seen[0])) == int(with_image)
        expected = HELD_TRANSFORM_FAILED_REPLY if withhold else "Reviewed terminal refusal."
        assert result["final_response"] == result["error"] == expected
        assert result["response_transformed"] and result["pre_transform_response"] is None
        assert "refused_turn_display_notice" not in result
        assert not streamed
        stored = db.get_messages_as_conversation(agent.session_id, include_row_ids=True)
        assert result["messages"] == stored[:-1] == history
        assert result["refused_turn_boundary_row_id"] == stored[-1]["_row_id"]
        assert stored[-1]["content"] == "Refused prompt"
    finally:
        db.close()
