"""Retry, fallback and nested/following policy requests use the correct declaration owner."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from gateway.session_authorities import owner_scope
from gateway.session_policy import policy_scope
from tests.gateway.test_session_tier_request_authority import authority_for, declaration, policy_for
from tests.agent.responses_test_support import response, text_item

pytest_plugins = ("tests.agent.responses_test_support",)


def offline_agent(**kwargs):
    from run_agent import AIAgent

    agent = AIAgent(model="private-model", provider="custom:paid", api_mode="codex_responses",
                    base_url="https://tiers.invalid/v1", api_key="offline-fixture", service_tier="priority",
                    quiet_mode=True, max_iterations=10, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, **kwargs)
    agent._cached_system_prompt = "Stable offline prompt."
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *args: None
    return agent


def capture_final(agent, monkeypatch):
    from tests.agent.responses_test_support import run_turn

    result, requests = run_turn(agent, monkeypatch, response(text_item("Done.")))
    assert result["completed"] and len(requests) == 1
    return requests[0].get("service_tier")


@pytest.mark.parametrize("frozen", [False, True])
def test_retry_requests_keep_frozen_declaration_after_live_edit(make_agent, monkeypatch, tmp_path, frozen):
    make_agent()
    config = declaration(frozen)
    (tmp_path / "config.yaml").write_text(json.dumps(declaration(not frozen)))
    authority, calls = authority_for(tmp_path), []
    with owner_scope(authority), policy_scope(policy_for(tmp_path, config), authority=authority):
        agent = offline_agent()

        def send(kwargs, **callbacks):
            calls.append(kwargs)
            assert len(calls) <= 3
            (tmp_path / "config.yaml").write_text(json.dumps(declaration(not frozen)))
            if len(calls) == 3:
                return response(text_item("Done."))
            raw = response(NS(type="function_call", id="fc", call_id="call", name="terminal",
                              status="incomplete", arguments='{"unfinished":'), status="incomplete")
            raw.incomplete_details = NS(reason="max_output_tokens")
            return raw

        monkeypatch.setattr(agent, "_interruptible_api_call", send)
        monkeypatch.setattr(agent, "_interruptible_streaming_api_call", send)
        result = agent.run_conversation("Offline task.")
    assert result["completed"] and len(calls) == 3
    assert all(call.get("service_tier") == ("priority" if frozen else None) for call in calls)


@pytest.mark.parametrize("allowed", [False, True])
def test_actual_fallback_request_rechecks_frozen_destination(make_agent, monkeypatch, tmp_path, allowed):
    make_agent()
    url = "https://backup.invalid/v1"
    config = declaration(True)
    config["providers"]["backup"] = {"base_url": url, "api_mode": "chat_completions",
                                      "api_key": "offline-fixture", "service_tiers": ["priority"] if allowed else []}
    live = copy.deepcopy(config)
    live["providers"]["paid"]["service_tiers"] = []
    live["providers"]["backup"]["service_tiers"] = [] if allowed else ["priority"]
    (tmp_path / "config.yaml").write_text(json.dumps(live))
    monkeypatch.setattr("agent.auxiliary_client._create_openai_client",
                        lambda **kw: NS(base_url=url, api_key="offline-fixture", _custom_headers={}))
    authority, calls = authority_for(tmp_path), []
    with owner_scope(authority), policy_scope(policy_for(tmp_path, config), authority=authority):
        agent = offline_agent()
        agent._fallback_chain = [{"provider": "custom:backup", "model": "fallback-model", "base_url": url,
                                  "api_mode": "chat_completions", "api_key": "offline-fixture"}]

        def send(kwargs, **callbacks):
            calls.append((agent.api_mode, kwargs))
            assert len(calls) <= 2
            if len(calls) == 1:
                error = RuntimeError("402 synthetic insufficient credits")
                error.status_code = 402
                raise error
            return NS(id="done", choices=[NS(message=NS(content="Done.", tool_calls=[]), finish_reason="stop")],
                      usage=NS(prompt_tokens=17, completion_tokens=3, total_tokens=20))

        monkeypatch.setattr(agent, "_interruptible_api_call", send)
        monkeypatch.setattr(agent, "_interruptible_streaming_api_call", send)
        result = agent.run_conversation("Offline task.")
    assert result["completed"] and len(calls) == 2
    assert calls[0][0] == "codex_responses" and calls[0][1].get("service_tier") == "priority"
    assert calls[1][0] == "chat_completions" and calls[1][1]["model"] == "fallback-model"
    assert calls[1][1].get("service_tier") == ("priority" if allowed else None)


def test_nested_and_unscoped_requests_restore_declarations(make_agent, monkeypatch, tmp_path):
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.models import resolve_fast_mode_overrides

    make_agent()
    (tmp_path / "config.yaml").write_text(json.dumps(declaration(False)))
    authority = authority_for(tmp_path)
    with owner_scope(authority), policy_scope(policy_for(tmp_path, declaration(True)), authority=authority):
        assert load_user_config_effective()["providers"]["paid"]["service_tiers"] == []
        assert resolve_fast_mode_overrides("private-model", provider="custom", requested_provider="custom:paid",
                                          base_url="https://tiers.invalid/v1", api_mode="codex_responses",
                                          config=declaration(False)) is None
        assert capture_final(offline_agent(), monkeypatch) == "priority"
        with policy_scope(policy_for(tmp_path, declaration(False)), authority=authority):
            assert capture_final(offline_agent(), monkeypatch) is None
        assert capture_final(offline_agent(), monkeypatch) == "priority"
    assert capture_final(offline_agent(), monkeypatch) is None


def test_following_policy_refreshes_between_turns_not_within_turn(make_agent, monkeypatch, tmp_path):
    from gateway import run
    from hermes_cli.service_tiers import get_configured_service_tiers

    make_agent()
    authority = authority_for(tmp_path)
    policy = policy_for(tmp_path, declaration(False), follows=True)
    live = declaration(True)
    reads = []

    def load(*args, **kwargs):
        reads.append(True)
        return copy.deepcopy(live)

    monkeypatch.setattr(run, "_load_gateway_config", load)
    with owner_scope(authority):
        with policy_scope(policy, authority=authority):
            live["providers"]["paid"]["service_tiers"] = []
            assert capture_final(offline_agent(), monkeypatch) == "priority"
            assert get_configured_service_tiers("private-model", provider="custom", requested_provider="custom:paid",
                                                base_url="https://tiers.invalid/v1", api_mode="codex_responses")
        with policy_scope(policy, authority=authority):
            assert capture_final(offline_agent(), monkeypatch) is None
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_concurrent_policies_in_one_profile_do_not_share_declarations(make_agent, monkeypatch, tmp_path):
    make_agent()
    authority = authority_for(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    agents = [offline_agent(), offline_agent()]

    async def turn(index, allowed):
        with owner_scope(authority), policy_scope(policy_for(tmp_path, declaration(allowed)), authority=authority):
            if index == 0:
                entered.set()
                await release.wait()
            else:
                await entered.wait()
                release.set()
            return capture_final(agents[index], monkeypatch)

    assert await asyncio.gather(turn(0, True), turn(1, False)) == ["priority", None]


@pytest.mark.parametrize("mismatch", ["endpoint", "protocol", "name", "missing"])
def test_actual_request_cannot_borrow_frozen_paid_capability(make_agent, monkeypatch, tmp_path, mismatch):
    make_agent()
    config = declaration(True)
    if mismatch == "missing":
        config["providers"]["paid"].pop("service_tiers")
    (tmp_path / "config.yaml").write_text(json.dumps(declaration(True)))
    authority = authority_for(tmp_path)
    calls = []
    with owner_scope(authority), policy_scope(policy_for(tmp_path, config), authority=authority):
        agent = offline_agent()
        if mismatch == "endpoint":
            agent.base_url = "https://other.invalid/v1"
        elif mismatch == "protocol":
            agent.api_mode = "chat_completions"
        elif mismatch == "name":
            agent.requested_provider = "custom:unknown"

        def send(kwargs, **callbacks):
            calls.append(kwargs)
            if agent.api_mode == "codex_responses":
                return response(text_item("Done."))
            return NS(id="done", choices=[NS(message=NS(content="Done.", tool_calls=[]), finish_reason="stop")],
                      usage=NS(prompt_tokens=17, completion_tokens=3, total_tokens=20))

        monkeypatch.setattr(agent, "_interruptible_api_call", send)
        monkeypatch.setattr(agent, "_interruptible_streaming_api_call", send)
        result = agent.run_conversation("Offline task.")
    assert result["completed"] and len(calls) == 1
    assert "service_tier" not in calls[0] and "speed" not in calls[0].get("extra_body", {})
