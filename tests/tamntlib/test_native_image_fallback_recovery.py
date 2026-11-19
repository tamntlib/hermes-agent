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


def test_native_image_fallback_length_final_is_transformed_before_flush(make_agent, monkeypatch, tmp_path):
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

    def send(kwargs, **callbacks):
        calls.append((agent.api_mode, kwargs))
        assert len(calls) <= 6
        if len(calls) == 1:
            return first
        if len(calls) == 2:
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
        assert len(calls) == 6
        assert [mode for mode, _ in calls] == ["codex_responses"] * 2 + ["chat_completions"] * 4
        assert len(seen) == 1
        assert len(media_paths(seen[0])) == 1 and media_paths(seen[0])[0].is_file()
        assert result["final_response"] == "Reviewed fallback partial."
        assert result["response_transformed"] is True
        assert result["pre_transform_response"] == seen[0]
        assert final_flushes and all(count == 1 and text == result["final_response"] for count, text in final_flushes)
        assert agent._session_db.get_messages_as_conversation(agent.session_id)[-1]["content"] == result["final_response"]
    finally:
        agent._session_db.close()
