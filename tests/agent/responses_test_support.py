"""Offline Responses fixtures shared by route-policy and image integration tests."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from run_agent import AIAgent


def response(*items, status="completed", response_id: str | None = "resp_1"):
    return NS(id=response_id, status=status, output=list(items), output_text="", model="gpt-5-codex",
              usage=NS(input_tokens=17, output_tokens=3, total_tokens=20))



def text_item(text="Done."):
    return NS(type="message", id="msg_1", status="completed", phase="final_answer",
              role="assistant", content=[NS(type="output_text", text=text)])



@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", dict)
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **kw: NS())
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *args, **kw: None)

    def make(native=False, tools=False):
        if tools:
            monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [{
                "type": "function", "function": {"name": "terminal", "description": "Offline fixture",
                "parameters": {"type": "object", "properties": {}}},
            }])
        agent = AIAgent(model="gpt-5-codex", provider="openai-codex" if native else "custom",
                        base_url="https://chatgpt.com/backend-api/codex" if native else "http://localhost:9999/v1",
                        api_mode="codex_responses", api_key="offline-fixture", quiet_mode=True,
                        max_iterations=3, skip_context_files=True, skip_memory=True,
                        save_trajectories=False)
        agent._cached_system_prompt = "You are helpful."
        agent.compression_enabled = False

        agent._cleanup_task_resources = lambda *args: None
        return agent
    return make



def run_turn(agent, monkeypatch, *responses, history=None):
    pending = iter(responses)
    requests = []

    def fake_call(kwargs, **callbacks):
        requests.append(kwargs)
        # A recovery request is a bug here; never allow a real provider fallback.
        return next(pending)

    monkeypatch.setattr(agent, "_interruptible_api_call", fake_call)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", fake_call)
    result = agent.run_conversation("Produce the requested output.", conversation_history=history)
    return result, requests
