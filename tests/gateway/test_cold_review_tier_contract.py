"""Cold reviews use their session tier without mutating the shared runner's tier."""
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource
from gateway.session_cold_review import cold_review_parent


@pytest.mark.parametrize("tier", [None, "priority"])
def test_cold_review_uses_explicit_session_tier(monkeypatch, tmp_path, tier):
    import hermes_yaml as yaml
    import gateway.run as gateway_run
    import run_agent

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"review": {
        "base_url": "https://review.invalid/v1", "api_mode": "codex_responses",
        "service_tiers": ["priority"],
    }}}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = {"provider": "custom", "requested_provider": "custom:review",
               "base_url": "https://review.invalid/v1", "api_mode": "codex_responses",
               "api_key": "synthetic", "request_overrides": {"temperature": 0.3}}
    runner = SimpleNamespace(
        _service_tier="priority", _provider_routing={}, _prefill_messages=[], _session_db=None,
        _resolve_turn_toolsets=lambda *a: ([], []),
        _resolve_session_service_tier=lambda **kw: tier,
        _resolve_session_agent_runtime=lambda **kw: ("review-model", runtime),
        _resolve_session_reasoning_config=lambda **kw: None, _refresh_fallback_model=lambda: None,
    )
    runner._resolve_turn_agent_config = MethodType(GatewayTurnMixin._resolve_turn_agent_config, runner)
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda *a: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", dict)
    build = MagicMock()
    monkeypatch.setattr(run_agent, "AIAgent", build)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="synthetic", user_id="synthetic")
    with cold_review_parent(runner, source, "session-key", "session-id"):
        expected = {"temperature": 0.3, **({"service_tier": "priority"} if tier else {})}
        assert build.call_args.kwargs["request_overrides"] == expected
        assert build.call_args.kwargs["service_tier"] == tier
    assert runner._service_tier == "priority"
    assert runtime["request_overrides"] == {"temperature": 0.3}
    # Omitted tier still inherits the runner default.
    assert runner._resolve_turn_agent_config("", "review-model", runtime)["request_overrides"] == {
        "temperature": 0.3, "service_tier": "priority"}
