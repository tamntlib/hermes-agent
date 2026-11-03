"""Session preference is projected against the live route, not a pinned request dict."""
from types import SimpleNamespace

from agent.fast_mode import effective_request_overrides


def agent(**changes):
    fields = dict(model="gpt-5.4", provider="openai", requested_provider="openai",
                  base_url="https://api.openai.com/v1", api_mode="codex_responses",
                  service_tier="priority", request_overrides={"extra_body": {"keep": 1}})
    fields.update(changes)
    return SimpleNamespace(**fields)


def test_static_preference_is_projected_for_a_fresh_agent():
    current = agent()
    overrides = effective_request_overrides(current)
    assert overrides == {"extra_body": {"keep": 1}, "service_tier": "priority"}
    assert current.request_overrides == {"extra_body": {"keep": 1}}


def test_custom_projection_uses_the_selected_named_provider(tmp_path, monkeypatch):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {
        "allowed": {"base_url": "https://cpa.example/v1", "transport": "codex_responses", "service_tiers": ["priority"]},
        "denied": {"base_url": "https://cpa.example/v1", "transport": "codex_responses"},
    }}))
    current = agent(provider="custom", requested_provider="custom:allowed", base_url="https://cpa.example/v1")
    assert effective_request_overrides(current)["service_tier"] == "priority"
    current.requested_provider = "custom:denied"
    current.request_overrides["service_tier"] = "priority"
    assert "service_tier" not in effective_request_overrides(current)
    assert current.service_tier == "priority"


def test_custom_session_modes_are_independent(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    from agent.fast_mode import begin_turn

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"cpa": {
        "base_url": "https://cpa.example/v1", "transport": "codex_responses",
        "service_tiers": ["priority"],
    }}}))
    route = dict(provider="custom", requested_provider="custom:cpa", base_url="https://cpa.example/v1")
    first = agent(**route, service_tier=None)
    second = agent(**route, service_tier=None)
    for selected, expected in [(None, None), ("priority", "priority"), (None, None)]:
        first.service_tier = selected
        assert effective_request_overrides(first).get("service_tier") == expected
        assert effective_request_overrides(second).get("service_tier") is None
    first.service_tier = "auto"
    begin_turn(first, [])
    assert effective_request_overrides(first)["service_tier"] == "priority"
    first._fast_until = 0
    assert "service_tier" not in effective_request_overrides(first)
    first.service_tier = "cold"
    begin_turn(first, [{"role": "user", "content": "existing history"}])
    assert "service_tier" not in effective_request_overrides(first)


def test_tier_projection_keeps_prompt_and_tools_unchanged(tmp_path, monkeypatch):
    from copy import deepcopy
    from agent.transports.codex import ResponsesApiTransport
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"cpa": {
        "base_url": "https://cpa.example/v1", "transport": "codex_responses",
        "service_tiers": ["priority"],
    }}}))
    current = agent(provider="custom", requested_provider="custom:cpa", base_url="https://cpa.example/v1")
    messages = [{"role": "system", "content": "Stable system prompt"}, {"role": "user", "content": "hi"}]
    original = deepcopy(messages)
    transport = ResponsesApiTransport()
    payloads = []
    for mode in (None, "priority", None):
        current.service_tier = mode
        kwargs = transport.build_kwargs(current.model, messages, provider=current.provider, base_url=current.base_url,
                                        request_overrides=effective_request_overrides(current))
        payloads.append(kwargs)
    assert payloads[1].pop("service_tier") == "priority"
    assert payloads[0] == payloads[1] == payloads[2]
    assert messages == original
