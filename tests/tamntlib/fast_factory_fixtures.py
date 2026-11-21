"""Real Desktop factory and SDK capture; only ancillary I/O is suppressed."""
import json
from types import SimpleNamespace

import httpx
import pytest
from anthropic import Anthropic
from openai import OpenAI

from persistence_assembly import open_persistence, profile_scope
from tui_gateway import server

MODEL = "diagnostic-alias"
URL = "https://fast-fixture.invalid/v1"


@pytest.fixture
def fast_factory(tmp_path, monkeypatch):
    config = {
        "model": {"provider": "custom:fixture", "default": MODEL},
        "agent": {"service_tier": "normal"},
        "providers": {"fixture": {
            "api": URL, "transport": "codex_responses", "api_key": "test",
            "models": {MODEL: {"context_length": 131072}},
            "extra_body": {"fixture_marker": "preserved"},
        }},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_IGNORE_RULES", "1")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", dict)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *_: [])
    monkeypatch.setattr(server, "_emit", lambda *_: None)
    monkeypatch.setattr(server, "_start_session_services", lambda *_: None)
    monkeypatch.setattr(server, "_wire_session_agent", lambda *_: False)
    db = open_persistence(profile_scope(home=tmp_path), eager=True)
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_sessions", {})
    agents = []

    def save():
        (tmp_path / "config.yaml").write_text(json.dumps(config))

    def make(sid="runtime", key="conversation", **kwargs):
        save()
        kwargs.setdefault("session_db", db)
        agent = server._make_agent(sid, key, **kwargs)
        agents.append(agent)
        return agent

    save()
    yield SimpleNamespace(config=config, db=db, make=make, save=save, home=tmp_path, agents=agents, request=sdk_request)
    for agent in agents:
        agent.close()
    db.close()


class RequestCaptured(BaseException):
    """Terminate SDK sending at the mock transport without inventing a reply."""


def sdk_request(agent):
    captured = []

    def receive(request):
        captured.append((json.loads(request.content), dict(request.headers)))
        raise RequestCaptured

    kwargs = agent._build_api_kwargs([{"role": "user", "content": "Offline fixture"}], [])
    kwargs["stream"] = True  # Match the production streaming call, including Anthropic's SDK guard.
    with httpx.Client(transport=httpx.MockTransport(receive)) as http:
        if agent.api_mode == "anthropic_messages":
            client = Anthropic(api_key="test", base_url=URL, http_client=http)
            send = client.messages.create
        else:
            client = OpenAI(api_key="test", base_url=URL, http_client=http)
            send = client.responses.create if agent.api_mode == "codex_responses" else client.chat.completions.create
        with pytest.raises(RequestCaptured):
            send(**kwargs)
    return captured[0]
