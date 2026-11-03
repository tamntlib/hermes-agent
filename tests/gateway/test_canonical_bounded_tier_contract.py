"""Every canonical Fast preference validates its owning route before a session write."""
import copy
import json
from pathlib import Path
from types import MethodType, SimpleNamespace as NS

import hermes_yaml as yaml
import pytest

from agent.fast_mode import NORMAL_TIER_WORDS, SERVICE_TIER_WORDS, parse_exact_service_tier, service_tier_word
from gateway import session_tui_config as controls
from gateway.run_config_loaders import GatewayConfigLoadersMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.service_tiers import turn_agent_config
from gateway.session_contract import SessionRef
from gateway.session_policy import LocalSessionPolicy
from gateway.session_state import SERVICE_TIER_UNSET
from hermes_constants import get_hermes_home
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_canonical_named_tier_contract import _config, _connection


@pytest.fixture
def canonical_route(monkeypatch, tmp_path):
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("Network is forbidden in tier controls")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ambient, owner = tmp_path / "ambient", tmp_path / "owner"
    ambient.mkdir()
    owner.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    (ambient / "config.yaml").write_text(yaml.safe_dump(_config()))

    def make(scope, tiers, *, current=None):
        cfg = _config()
        cfg["agent"]["service_tier"] = current
        cfg["providers"]["paid"]["service_tiers"] = tiers
        opposite = _config()
        opposite["agent"]["service_tier"] = current
        opposite["providers"]["paid"]["service_tiers"] = [] if tiers else ["priority", "ultrafast"]
        (owner / "config.yaml").write_text(yaml.safe_dump(opposite if scope == "frozen" else cfg))
        route = dict(model="private-model", provider="custom", requested_provider="custom:paid",
                     base_url="https://tiers.invalid/v1", api_mode="codex_responses")
        agent = NS(**route, service_tier="ultrafast", request_overrides={"temperature": 0.3})
        state = NS(conversation=NS(service_tier_override=SERVICE_TIER_UNSET, model_override=route))
        runner = NS(config=NS(multiplex_profiles=True), _service_tier="ultrafast",
                    request_overrides={"temperature": 0.2}, _session_state=lambda key: state,
                    _peek_session_state=lambda key: state, _resolve_session_key_or_none=lambda source, key: key,
                    _rehydrate_session_model_override=lambda key: None,
                    _resolve_session_service_tier=lambda **kw: current)
        runner._set_session_service_tier_override = MethodType(
            GatewayConfigLoadersMixin._set_session_service_tier_override, runner)
        runner._resolve_session_agent_runtime = MethodType(GatewayTurnMixin._resolve_session_agent_runtime, runner)
        policy = None
        if scope in {"pending", "frozen"}:
            policy = LocalSessionPolicy("gui", "desktop", str(owner), "private-model", (),
                                        json.dumps(opposite if scope == "pending" else cfg),
                                        json.dumps({"follow_profile_config": scope == "pending"}), "{}")
            # The resident used another route. Neither its permission nor the ambient one owns this selection.
            agent.requested_provider = "custom:denied"
            agent.api_mode = "anthropic_messages"
        authority = NS(profile_id=str(owner), runner=runner, db=NS(db_path=owner / "state.db"),
                       sessions={"session": NS(route="route", source=None)},
                       agent=lambda ref: None if scope == "cold" else agent)
        monkeypatch.setattr(controls, "_authorize_write", lambda *a: policy)
        return NS(connection=_connection(authority), ref=SessionRef(str(owner), "session"),
                  state=state, runner=runner, agent=agent, route=route, cfg=cfg, ambient=ambient)

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["resident", "pending", "cold", "frozen"])
@pytest.mark.parametrize("tiers", [["priority"], [], ["ultrafast"]], ids=["priority", "denied", "ultrafast_only"])
@pytest.mark.parametrize("word", ["normal", "fast", "ultrafast", "auto", "cold", "status", "toggle"])
async def test_every_preference_validates_before_mutating_owning_session(canonical_route, scope, tiers, word):
    case = canonical_route(scope, tiers)
    before_agent = copy.deepcopy(vars(case.agent))
    call = controls.config_set(case.connection, case.ref, {"key": "fast", "value": word})
    tier = parse_exact_service_tier("fast" if word == "toggle" else "normal" if word == "status" else word) or None
    required = "priority" if tier in {"auto", "cold"} else tier
    if required and required not in tiers:
        with pytest.raises(RuntimeStoreError, match="fast_unavailable"):
            await call
        assert case.state.conversation.service_tier_override is SERVICE_TIER_UNSET
        # Even an ineligible route remains controllable at Normal and inspectable.
        assert await controls.config_set(case.connection, case.ref, {"key": "fast", "value": "status"}) == {
            "key": "fast", "value": "normal"}
        assert await controls.config_set(case.connection, case.ref, {"key": "fast", "value": "normal"}) == {
            "key": "fast", "value": "normal"}
        assert case.state.conversation.service_tier_override is None
    else:
        assert await call == {"key": "fast", "value": service_tier_word(tier)}
        if word == "status":
            assert case.state.conversation.service_tier_override is SERVICE_TIER_UNSET
        else:
            assert case.state.conversation.service_tier_override == tier
        if tier in {"auto", "cold"}:
            projected = turn_agent_config(case.runner, "offline", "private-model", case.route, tier=tier)
            assert projected["request_overrides"] == {}
    assert vars(case.agent) == before_agent
    assert case.runner._service_tier == "ultrafast"
    assert case.runner.request_overrides == {"temperature": 0.2}
    assert get_hermes_home() == case.ambient


@pytest.mark.asyncio
@pytest.mark.parametrize("word", sorted((set(SERVICE_TIER_WORDS) | set(NORMAL_TIER_WORDS)) - {""}))
async def test_all_supported_mode_aliases_preserve_preference(canonical_route, word):
    case = canonical_route("resident", ["priority", "ultrafast"])
    tier = parse_exact_service_tier(word) or None
    assert await controls.config_set(case.connection, case.ref, {"key": "fast", "value": word}) == {
        "key": "fast", "value": service_tier_word(tier)}
    assert case.state.conversation.service_tier_override == tier


@pytest.mark.asyncio
@pytest.mark.parametrize("word,current,expected", [("", None, "priority"), ("toggle", "priority", None)])
async def test_toggle_retains_normal_and_priority_semantics(canonical_route, word, current, expected):
    case = canonical_route("resident", ["priority"], current=current)
    assert await controls.config_set(case.connection, case.ref, {"key": "fast", "value": word}) == {
        "key": "fast", "value": service_tier_word(expected)}
    assert case.state.conversation.service_tier_override == expected
