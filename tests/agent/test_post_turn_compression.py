"""Post-reply compression only adds an earlier trigger to the existing compressor."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _agent():
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(model="test/model", config_context_length=100_000, threshold_percent=0.90)
    return SimpleNamespace(compression_enabled=True, context_compressor=compressor,
                           api_mode="chat_completions", _cached_system_prompt="fixture", tools=[])


@pytest.mark.parametrize("tokens,expected", [(87_999, False), (88_000, True), (89_000, True), (90_000, True)])
def test_post_turn_trigger_is_two_window_percentage_points_before_hard_limit(tokens, expected):
    from agent.post_turn_compression import post_turn_compression_due

    agent = _agent()
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}] * 4
    with patch("agent.turn_context._preflight_request_tokens", return_value=tokens):
        assert post_turn_compression_due(agent, history, {"compression": {"threshold_buffer": 0.02}}) is expected
    assert agent.context_compressor.threshold_tokens == 90_000  # the hard trigger stays untouched


def test_existing_configs_get_the_default_two_point_buffer():
    from agent.post_turn_compression import post_turn_compression_due
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["threshold_buffer"] == 0.02
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}] * 4
    with patch("agent.turn_context._preflight_request_tokens", return_value=88_000):
        assert post_turn_compression_due(_agent(), history, {}) is True


@pytest.mark.parametrize("buffer", [None, True, False, -0.1, 1, "bad", float("nan"), float("inf")])
def test_disabled_or_invalid_buffer_never_prices_or_compresses(buffer):
    from agent.post_turn_compression import post_turn_compression_due
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}] * 4
    with patch("agent.turn_context._preflight_request_tokens", side_effect=AssertionError("must not price")):
        assert post_turn_compression_due(_agent(), history, {"compression": {"threshold_buffer": buffer}}) is False


@pytest.mark.parametrize("tokens,expected", [(89_999, False), (90_000, True)])
def test_zero_buffer_waits_until_hard_trigger_after_reply(tokens, expected):
    from agent.post_turn_compression import post_turn_compression_due
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}] * 4
    with patch("agent.turn_context._preflight_request_tokens", return_value=tokens):
        assert post_turn_compression_due(_agent(), history, {"compression": {"threshold_buffer": 0}}) is expected


@pytest.mark.parametrize("blocked", ["disabled", "short", "external", "app_server", "native_responses"])
def test_ineligible_sessions_are_left_to_their_existing_policy(blocked):
    from agent.post_turn_compression import post_turn_compression_due
    agent = _agent()
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}] * 4
    changes = {
        "disabled": {"compression_enabled": False},
        "short": {},
        "external": {"context_compressor": SimpleNamespace()},
        "app_server": {"api_mode": "codex_app_server"},
        "native_responses": {"codex_responses_native_compaction": True},
    }
    agent.__dict__.update(changes[blocked])
    history = history[:2] if blocked == "short" else history
    with patch("agent.turn_context._preflight_request_tokens", side_effect=AssertionError("must not price")):
        assert post_turn_compression_due(agent, history, {}) is False


def test_absolute_cap_is_used_without_changing_the_cap():
    from agent.post_turn_compression import post_turn_compression_due
    agent = _agent()
    agent.context_compressor.threshold_tokens_cap = 50_000
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}] * 4
    with patch("agent.turn_context._preflight_request_tokens", return_value=48_000):
        assert post_turn_compression_due(agent, history, {}) is True
    assert agent.context_compressor.threshold_tokens == 50_000


def test_real_provider_anchor_counts_the_completed_reply_without_double_charging():
    from agent.post_turn_compression import post_turn_compression_due
    from agent.usage_anchor import capture_usage_anchor
    agent = _agent()
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "before"},
               {"role": "user", "content": "current question"}]
    agent._usage_anchor = capture_usage_anchor(87_000, 1_000, history)
    history.append({"role": "assistant", "content": "completed answer"})
    assert post_turn_compression_due(agent, history, {}) is True
    agent._usage_anchor = capture_usage_anchor(86_000, 1_000, history[:-1])
    assert post_turn_compression_due(agent, history, {}) is False


@pytest.mark.parametrize("tokens,expected", [(7_999, False), (8_000, True), (9_000, True)])
def test_a_cap_smaller_than_the_buffer_still_compresses_at_the_hard_cap(tokens, expected):
    from agent.post_turn_compression import post_turn_compression_due
    from agent.context_compressor import ContextCompressor
    agent = _agent()
    agent.context_compressor = ContextCompressor(model="test/model", config_context_length=1_000_000,
                                                threshold_percent=0.90, threshold_tokens_cap=8_000)
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}] * 4
    with patch("agent.turn_context._preflight_request_tokens", return_value=tokens):
        assert post_turn_compression_due(agent, history, {}) is expected
