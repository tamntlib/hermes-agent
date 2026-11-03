"""Canonical Fast controls and task validation retain exact route ownership."""
import json
from types import SimpleNamespace

import hermes_yaml as yaml
import pytest

from gateway.session_contract import SessionRef
from gateway.session_local import _reasoning_fields
from gateway.session_policy import LocalSessionPolicy
from gateway.session_tui_config import config_set
from hermes_state_runtime import RuntimeStoreError
from tools.delegate_tool_tuning import task_service_tier


def _connection(authority):
    """A capability-bearing unit-test viewer with the v0.23.0 publication seam."""
    actor = SimpleNamespace(capabilities={"session:control"})

    def authorize(who, ref, capability):
        assert who is actor and capability in actor.capabilities
        assert ref.session_id in authority.sessions

    authority.authorize = authorize
    authority.published = []
    for live in authority.sessions.values():
        live.event_stream = SimpleNamespace(publish=lambda *args, **kwargs: authority.published.append((args, kwargs)))
    return SimpleNamespace(authority=authority, actor=actor)


@pytest.mark.parametrize("declared,expected", [
    (["priority", "ultrafast"], ["normal", "fast", "auto", "cold", "ultrafast"]),
    (["ultrafast"], ["normal", "ultrafast"]),
    ([], []),
])
def test_speed_options_use_frozen_named_declarations(monkeypatch, tmp_path, declared, expected):
    from gateway.session_tui_config import service_tier_options

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _config()
    config["providers"]["paid"]["service_tiers"] = declared
    policy = LocalSessionPolicy("gui", "desktop", str(tmp_path), "private-model", (),
                                json.dumps(config), "{}", "{}")
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                             _peek_session_state=lambda key: None)
    authority = SimpleNamespace(profile_id=str(tmp_path), runner=runner,
                                db=SimpleNamespace(db_path=tmp_path / "state.db"),
                                sessions={"session": SimpleNamespace(route="route", source=None)},
                                agent=lambda ref: None)
    assert service_tier_options(_connection(authority),
                                SessionRef(str(tmp_path), "session"), policy) == expected



def _config(identity="custom:paid"):
    return {"model": {"default": "private-model", "provider": identity},
            "providers": {"paid": {"base_url": "https://tiers.invalid/v1",
                                   "api_mode": "codex_responses", "service_tiers": ["priority", "ultrafast"]},
                          "denied": {"base_url": "https://tiers.invalid/v1",
                                     "api_mode": "codex_responses", "service_tiers": []}},
            "agent": {"service_tier": "priority"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("resident", [False, True])
@pytest.mark.parametrize("word", ["fast", "ultrafast"])
@pytest.mark.parametrize("follows", [False, True])
async def test_canonical_fast_validates_pending_frozen_route_and_displays_it(
    monkeypatch, tmp_path, resident, word, follows,
):
    from gateway import session_tui_config
    # Ambient/profile entry denies; the session froze a paid route and owns its declaration.
    ambient = _config("custom:denied")
    ambient["providers"]["paid"]["service_tiers"] = []
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_config() if follows else ambient))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    policy = LocalSessionPolicy("gui", "desktop", str(tmp_path), "private-model", (),
                                json.dumps(ambient if follows else _config()),
                                json.dumps({"follow_profile_config": follows}), "{}")
    stale = SimpleNamespace(model="old-model", provider="custom", requested_provider="custom:denied",
                            base_url="https://tiers.invalid/v1", api_mode="chat_completions",
                            service_tier=None, reasoning_config={}) if resident else None
    writes = []
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                             _peek_session_state=lambda key: None,
                             _set_session_service_tier_override=lambda *args: writes.append(args))
    authority = SimpleNamespace(profile_id=str(tmp_path), runner=runner,
                                db=SimpleNamespace(db_path=tmp_path / "state.db"),
                                sessions={"session": SimpleNamespace(route="route", source=None)},
                                agent=lambda ref: stale)
    connection = _connection(authority)
    ref = SessionRef(str(tmp_path), "session")
    monkeypatch.setattr(session_tui_config, "_authorize_write", lambda *args: policy)
    result = await config_set(connection, ref, {"key": "fast", "value": word})
    assert result == {"key": "fast", "value": word}
    assert writes == [("route", "ultrafast" if word == "ultrafast" else "priority")]
    # Lazy/pending route display must agree with the next-turn config.set gate.
    fields = _reasoning_fields(None, policy, "private-model", authority)
    assert fields["fast"] is True


@pytest.mark.asyncio
async def test_canonical_url_only_route_never_borrows_shared_url_permission(monkeypatch, tmp_path):
    from gateway import session_tui_config
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_config()))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    writes = []
    agent = SimpleNamespace(model="private-model", provider="custom", requested_provider="custom",
                            base_url="https://tiers.invalid/v1", api_mode="codex_responses")
    runner = SimpleNamespace(_resolve_session_service_tier=lambda **kw: None,
                             _set_session_service_tier_override=lambda *a: writes.append(a))
    authority = SimpleNamespace(profile_id=str(tmp_path), runner=runner, agent=lambda ref: agent,
                                sessions={"session": SimpleNamespace(route="route", source=None)})
    monkeypatch.setattr(session_tui_config, "_authorize_write", lambda *a: None)
    with pytest.raises(RuntimeStoreError, match="fast_unavailable"):
        await config_set(_connection(authority), SessionRef(str(tmp_path), "session"),
                         {"key": "fast", "value": "fast"})
    assert writes == []


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("word", ["fast", "ultrafast"])
def test_explicit_task_tier_uses_child_exact_identity_and_protocol(monkeypatch, tmp_path, pinned, word):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_config()))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = SimpleNamespace(model="private-model", provider="custom", requested_provider="custom:paid",
                             base_url="https://tiers.invalid/v1", api_mode="codex_responses", api_key="synthetic",
                             request_overrides={"temperature": 0.2})
    creds = {"provider": "custom:paid", "model": "private-model", "base_url": parent.base_url,
             "api_mode": parent.api_mode, "api_key": "synthetic"} if pinned else {}
    result = task_service_tier(word, creds, parent)
    assert result["task_tier_overrides"] == {"service_tier": "ultrafast" if word == "ultrafast" else "priority"}
    assert parent.request_overrides == {"temperature": 0.2}
    # A URL-only pin cannot inherit the parent's named paid authorization, nor can a wrong wire.
    for bad in ({"provider": "custom", "base_url": parent.base_url, "api_mode": parent.api_mode},
                {"provider": "custom:paid", "base_url": parent.base_url, "api_mode": "anthropic_messages"},
                {"provider": "custom:denied", "base_url": parent.base_url, "api_mode": parent.api_mode}):
        with pytest.raises(ValueError, match="not available"):
            task_service_tier(word, bad, parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["paid", "denied", "url_only", "wrong_protocol"])
@pytest.mark.parametrize("word", ["fast", "ultrafast"])
async def test_cold_messaging_fast_resolves_exact_route_in_owning_profile(monkeypatch, tmp_path, selection, word):
    from types import MethodType
    from gateway.config import Platform
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session import SessionSource
    from gateway import session_tui_config
    from hermes_constants import get_hermes_home

    ambient, owner = tmp_path / "ambient", tmp_path / "owner"
    ambient.mkdir()
    owner.mkdir()
    other = _config()
    other["providers"]["paid"]["service_tiers"] = []
    other["providers"]["denied"]["service_tiers"] = ["priority"]
    (ambient / "config.yaml").write_text(yaml.safe_dump(other))
    (owner / "config.yaml").write_text(yaml.safe_dump(_config()))
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    requested = "custom" if selection == "url_only" else "custom:denied" if selection == "denied" else "custom:paid"
    override = dict(model="private-model", provider="custom", requested_provider=requested,
                    base_url="https://tiers.invalid/v1", api_mode="anthropic_messages" if selection == "wrong_protocol" else "codex_responses",
                    api_key="offline-synthetic", credential_pool=object())
    resolutions, writes = [], []

    def rehydrate(route):
        resolutions.append((route, get_hermes_home()))

    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                             _resolve_session_key_or_none=lambda source, key: key,
                             _rehydrate_session_model_override=rehydrate,
                             _peek_session_state=lambda key: SimpleNamespace(conversation=SimpleNamespace(model_override=override)),
                             _resolve_session_service_tier=lambda **kw: None,
                             _set_session_service_tier_override=lambda *args: writes.append(args))
    runner._resolve_session_agent_runtime = MethodType(GatewayTurnMixin._resolve_session_agent_runtime, runner)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="synthetic", user_id="synthetic")
    authority = SimpleNamespace(profile_id=str(owner), runner=runner, agent=lambda ref: None,
                                sessions={"session": SimpleNamespace(route="cold-route", source=source)})
    monkeypatch.setattr(session_tui_config, "_authorize_write", lambda *args: None)
    call = config_set(_connection(authority), SessionRef(str(owner), "session"),
                      {"key": "fast", "value": word})
    if selection == "paid":
        assert await call == {"key": "fast", "value": word}
        assert writes == [("cold-route", "ultrafast" if word == "ultrafast" else "priority")]
    else:
        with pytest.raises(RuntimeStoreError, match="fast_unavailable"):
            await call
        assert writes == []
    assert resolutions == [("cold-route", owner)]
    assert get_hermes_home() == ambient
    assert override["requested_provider"] == requested


@pytest.mark.asyncio
@pytest.mark.parametrize("follows", [False, True])
@pytest.mark.parametrize("selection", ["denied", "wrong_protocol"])
@pytest.mark.parametrize("word", ["fast", "ultrafast"])
async def test_pending_local_fast_cannot_borrow_resident_or_ambient_permission(
    monkeypatch, tmp_path, follows, selection, word,
):
    from gateway import session_tui_config
    from hermes_constants import get_hermes_home

    ambient, owner = tmp_path / "ambient", tmp_path / "owner"
    ambient.mkdir()
    owner.mkdir()
    (ambient / "config.yaml").write_text(yaml.safe_dump(_config()))
    pending = _config("custom:denied" if selection == "denied" else "custom:paid")
    if selection == "wrong_protocol":
        pending["model"]["api_mode"] = "anthropic_messages"
    (owner / "config.yaml").write_text(yaml.safe_dump(pending))
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    # Frozen sessions use their pending receipt; following sessions resolve the owner's
    # current profile instead. Both supersede a resident agent still on the old paid route.
    frozen = _config() if follows else pending
    policy = LocalSessionPolicy("gui", "desktop", str(owner), "private-model", (),
                                json.dumps(frozen), json.dumps({"follow_profile_config": follows}), "{}")
    resident = SimpleNamespace(model="private-model", provider="custom", requested_provider="custom:paid",
                               base_url="https://tiers.invalid/v1", api_mode="codex_responses")
    writes = []
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True), _peek_session_state=lambda key: None,
                             _set_session_service_tier_override=lambda *args: writes.append(args))
    authority = SimpleNamespace(profile_id=str(owner), db=SimpleNamespace(db_path=owner / "state.db"),
                                runner=runner, agent=lambda ref: resident,
                                sessions={"session": SimpleNamespace(route="pending-route", source=None)})
    monkeypatch.setattr(session_tui_config, "_authorize_write", lambda *args: policy)
    with pytest.raises(RuntimeStoreError, match="fast_unavailable"):
        await config_set(_connection(authority), SessionRef(str(owner), "session"),
                         {"key": "fast", "value": word})
    assert writes == []
    fields = _reasoning_fields(None, policy, "private-model", authority)
    assert fields["fast"] is False
    assert get_hermes_home() == ambient
