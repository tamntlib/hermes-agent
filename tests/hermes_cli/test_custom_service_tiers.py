"""Explicit custom-provider tier support, distinct from a session's chosen tier."""
import re
from typing import Any

import pytest

from hermes_cli.config import get_compatible_custom_providers


@pytest.mark.parametrize("tiers", [["priority"], ["priority", "ultrafast"], []])
def test_service_tiers_survive_provider_normalization(tiers):
    entries = get_compatible_custom_providers({"providers": {
        "cpa": {"base_url": "https://cpa.example/v1", "service_tiers": tiers},
    }})
    assert len(entries) == 1
    assert entries[0].get("service_tiers") == tiers


def test_named_provider_declares_tiers_for_an_exact_model_alias():
    from hermes_cli.service_tiers import get_configured_service_tiers

    config = {"providers": {"cpa": {
        "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
        "service_tiers": ["priority", "ultrafast"],
    }}}
    assert get_configured_service_tiers(
        "my-exact-alias", provider="custom", requested_provider="custom:cpa",
        base_url="https://cpa.example/v1", api_mode="codex_responses", config=config,
    ) == ("priority", "ultrafast")


@pytest.mark.parametrize("model_tiers", [["ultrafast"], []])
def test_model_declaration_replaces_provider_tiers(model_tiers):
    from hermes_cli.service_tiers import get_configured_service_tiers

    config = {"providers": {"cpa": {
        "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
        "service_tiers": ["priority"],
        "models": {"Alias": {"service_tiers": model_tiers}},
    }}}
    route: dict[str, Any] = dict(provider="custom", requested_provider="cpa",
                 base_url="https://cpa.example/v1", api_mode="codex_responses", config=config)
    assert get_configured_service_tiers("Alias", **route) == tuple(model_tiers)
    assert get_configured_service_tiers("alias", **route) == ("priority",)


@pytest.mark.parametrize("bad", [
    "priority", True, None, 1, ("priority",), {},
    ["fast"], ["auto"], ["cold"], ["Priority"], [" priority"], [False], [{}],
])
@pytest.mark.parametrize("level", ["provider", "model"])
def test_invalid_declarations_survive_normalization_and_raise_with_path(bad, level):
    from hermes_cli.service_tiers import get_configured_service_tiers

    entry = {
        "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
        "service_tiers": ["priority"],
        "models": {"Alias": {"service_tiers": ["ultrafast"]}},
    }
    target = entry if level == "provider" else entry["models"]["Alias"]
    target["service_tiers"] = bad
    config = {"providers": {"cpa": entry}}
    normalized = get_compatible_custom_providers(config)[0]
    preserved = normalized if level == "provider" else normalized["models"]["Alias"]
    assert preserved["service_tiers"] == bad
    path = "providers.cpa" + (".models.Alias" if level == "model" else "") + ".service_tiers"
    with pytest.raises(ValueError, match=re.escape(path)):
        get_configured_service_tiers(
            "Alias", provider="custom", requested_provider="cpa",
            base_url="https://cpa.example/v1", api_mode="codex_responses", config=config,
        )


@pytest.mark.parametrize("changes", [
    {"base_url": "https://cpa.example/v2"},
    {"base_url": "https://cpa.example/tenant/v1"},
    {"base_url": "http://cpa.example/v1"},
    {"base_url": "https://cpa.example:8443/v1"},
    {"base_url": "https://cpa.example/V1"},
    {"base_url": "https://cpa.example/v1?account=other"},
    {"base_url": None},
    {"api_mode": "chat_completions"},
    {"api_mode": None},
    {"requested_provider": "custom:missing"},
    {"requested_provider": "CPA"},
])
def test_named_provider_route_mismatch_fails_closed(changes):
    from hermes_cli.service_tiers import get_configured_service_tiers

    route: dict[str, Any] = {
        "provider": "custom", "requested_provider": "cpa",
        "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
        "config": {"providers": {"cpa": {
            "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
            "service_tiers": ["priority"],
        }}},
    }
    route.update(changes)
    assert get_configured_service_tiers("gpt-5.4", **route) == ()


def test_same_endpoint_sibling_never_supplies_tiers():
    from hermes_cli.service_tiers import get_configured_service_tiers

    shared = {"name": "Same display", "base_url": "https://cpa.example/v1",
              "api_mode": "codex_responses"}
    config = {"providers": {
        "paid": {**shared, "service_tiers": ["priority"]},
        "plain": {**shared, "service_tiers": []},
        "unset": shared,
    }}
    route: dict[str, Any] = dict(provider="custom", base_url="https://cpa.example/v1",
                                api_mode="codex_responses", config=config)
    assert get_configured_service_tiers("Alias", requested_provider="plain", **route) == ()
    assert get_configured_service_tiers("Alias", requested_provider="unset", **route) is None
    assert get_configured_service_tiers("Alias", requested_provider="paid", **route) == ("priority",)


@pytest.mark.parametrize("identity", [None, "", "custom", "auto"])
def test_anonymous_identity_cannot_authorize_configured_opt_in(identity):
    from hermes_cli.service_tiers import get_configured_service_tiers

    entry = {"base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
             "service_tiers": ["priority"]}
    config = {"model": {"provider": "paid"}, "providers": {
        "paid": entry, "custom": entry, "auto": entry,
    }}
    assert not get_configured_service_tiers(
        "Alias", provider="custom", requested_provider=identity,
        base_url="https://cpa.example/v1", api_mode="codex_responses", config=config,
    )


def test_disabled_named_provider_cannot_authorize_opt_in():
    from hermes_cli.service_tiers import get_configured_service_tiers

    assert get_configured_service_tiers(
        "Alias", provider="custom", requested_provider="cpa",
        base_url="https://cpa.example/v1", api_mode="codex_responses",
        config={"providers": {"cpa": {
            "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
            "service_tiers": ["priority"], "enabled": False,
        }}},
    ) == ()


def test_service_tiers_load_from_bound_profile_not_launch_home(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    from hermes_cli.service_tiers import get_configured_service_tiers
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    profiles = [("one", ["priority"]), ("two", ["ultrafast"])]
    for name, tiers in profiles:
        home = tmp_path / name
        home.mkdir()
        atomic_config_write(home / "config.yaml", {"providers": {"cpa": {
            "api": "https://CPA.EXAMPLE:443/v1/", "transport": "responses",
            "service_tiers": tiers,
        }}})
        token = set_hermes_home_override(home)
        try:
            assert get_configured_service_tiers(
                "Alias", provider="custom", requested_provider="custom:cpa",
                base_url="https://cpa.example/v1", api_mode="codex_responses",
            ) == tuple(tiers)
        finally:
            reset_hermes_home_override(token)


@pytest.mark.parametrize("declared", [["priority"], ["ultrafast"], ["priority", "ultrafast"], []])
@pytest.mark.parametrize("api_mode", [
    "codex_responses", "chat_completions", "anthropic_messages", "bedrock_converse", "codex_app_server",
])
@pytest.mark.parametrize("tier", [None, "priority", "ultrafast", "normal"])
def test_custom_gate_maps_only_the_independently_declared_tier(declared, api_mode, tier):
    from hermes_cli.models import resolve_fast_mode_overrides

    selected = "priority" if tier is None else tier
    expected = None
    if selected in declared:
        if api_mode in {"codex_responses", "chat_completions"}:
            expected = {"service_tier": selected}
        elif api_mode == "anthropic_messages" and selected == "priority":
            expected = {"speed": "fast"}
    assert resolve_fast_mode_overrides(
        "unlisted-alias", provider="custom", requested_provider="custom:cpa",
        base_url="https://cpa.example/v1", api_mode=api_mode, tier=tier,
        config={"providers": {"cpa": {
            "base_url": "https://cpa.example/v1", "api_mode": api_mode,
            "service_tiers": declared,
        }}},
    ) == expected


@pytest.mark.parametrize("entry", [{}, {"api_key": "unused"}, {
    "base_url": "https://api.openai.com/v1", "api_mode": "codex_responses",
}])
def test_absent_declarations_keep_builtin_policy_even_with_partial_provider_config(entry):
    from hermes_cli.models import resolve_fast_mode_overrides
    from hermes_cli.service_tiers import get_configured_service_tiers

    route: dict[str, Any] = dict(
        provider="openai", requested_provider="openai", base_url="https://api.openai.com/v1",
        api_mode="codex_responses", config={"providers": {"openai": entry}},
    )
    assert get_configured_service_tiers("gpt-5.4", **route) is None
    assert resolve_fast_mode_overrides("gpt-5.4", **route) == {"service_tier": "priority"}


@pytest.mark.parametrize("requested", ["missing", "custom:missing"])
def test_named_mismatch_cannot_fall_back_to_runtime_builtin_provider(requested):
    from hermes_cli.models import resolve_fast_mode_overrides

    assert resolve_fast_mode_overrides(
        "gpt-5.4", provider="openai", requested_provider=requested,
        base_url="https://api.openai.com/v1", api_mode="codex_responses", config={},
    ) is None


def test_legacy_named_provider_tiers_use_legacy_path_and_never_shadow_provider_key():
    from hermes_cli.service_tiers import get_configured_service_tiers

    entry = {"name": "cpa", "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
             "models": [{"id": "Alias", "service_tiers": ["ultrafast"]}]}
    config: dict[str, Any] = {"custom_providers": [entry]}
    route: dict[str, Any] = dict(provider="custom", requested_provider="custom:cpa",
                                base_url="https://cpa.example/v1", api_mode="codex_responses", config=config)
    assert get_configured_service_tiers("Alias", **route) == ("ultrafast",)
    entry["models"][0]["service_tiers"] = None
    with pytest.raises(ValueError, match=r"custom_providers\[0\].models.Alias.service_tiers"):
        get_configured_service_tiers("Alias", **route)
    config["providers"] = {"cpa": {
        "base_url": "https://cpa.example/v1", "api_mode": "codex_responses", "service_tiers": [],
    }}
    assert get_configured_service_tiers("Alias", **route) == ()


@pytest.mark.parametrize("metadata", [None, "description", 123])
def test_non_mapping_model_metadata_inherits_provider_declaration(metadata):
    from hermes_cli.service_tiers import get_configured_service_tiers

    assert get_configured_service_tiers(
        "Alias", provider="custom", requested_provider="cpa",
        base_url="https://cpa.example/v1", api_mode="codex_responses",
        config={"providers": {"cpa": {
            "base_url": "https://cpa.example/v1", "api_mode": "codex_responses",
            "service_tiers": ["priority"], "models": {"Alias": metadata},
        }}},
    ) == ("priority",)


@pytest.mark.parametrize("identity", [None, "", "auto"])
def test_automatic_first_party_resolution_keeps_builtin_policy(identity):
    from hermes_cli.models import resolve_fast_mode_overrides

    assert resolve_fast_mode_overrides(
        "gpt-5.4", provider="openai", requested_provider=identity,
        base_url="https://api.openai.com/v1", api_mode="codex_responses", config={},
    ) == {"service_tier": "priority"}


@pytest.mark.parametrize("declaration", [[], ["ultrafast"]])
def test_explicit_first_party_declaration_overrides_builtin_priority(declaration):
    from hermes_cli.models import resolve_fast_mode_overrides

    route: dict[str, Any] = dict(
        provider="openai", requested_provider="openai",
        base_url="https://api.openai.com/v1", api_mode="codex_responses",
        config={"providers": {"openai": {
            "base_url": "https://api.openai.com/v1", "api_mode": "codex_responses",
            "service_tiers": declaration,
        }}},
    )
    assert resolve_fast_mode_overrides("gpt-5.4", **route) is None
    expected = {"service_tier": "ultrafast"} if declaration else None
    assert resolve_fast_mode_overrides("gpt-5.4", tier="ultrafast", **route) == expected


def test_invalid_model_declaration_is_not_lost_when_provider_url_is_missing():
    from hermes_cli.models import resolve_fast_mode_overrides

    with pytest.raises(ValueError, match=r"providers.openai.models.gpt-5.4.service_tiers"):
        resolve_fast_mode_overrides(
            "gpt-5.4", provider="openai", requested_provider="openai",
            base_url="https://api.openai.com/v1", api_mode="codex_responses",
            config={"providers": {"openai": {"models": {"gpt-5.4": {"service_tiers": None}}}}},
        )
