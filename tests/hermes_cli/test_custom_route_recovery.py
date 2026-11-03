"""Routing identity recovery must not infer permission to request paid tiers."""
import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli import runtime_provider_custom as custom


@pytest.fixture
def routes(monkeypatch):
    config = {"model": {"provider": "custom-anthropic_messages"}, "providers": {
        "custom-codex_responses": {
            "base_url": "https://gateway.example/v1",
            "api_mode": "codex_responses",
            "models": {"gpt6": {"service_tiers": ["priority"]}},
        },
        "custom-anthropic_messages": {
            "base_url": "https://gateway.example",
            "api_mode": "anthropic_messages",
            "models": {"gpt6": {}},
        },
    }}
    monkeypatch.setattr(rp, "load_config", lambda: config)
    monkeypatch.setattr(rp, "_get_model_config", lambda: config["model"])

    def no_credentials(*args, **kwargs):
        pytest.fail("Identity recovery must not resolve credentials")

    monkeypatch.setattr(custom, "get_secret_str", no_credentials)
    monkeypatch.setattr(rp, "load_pool", no_credentials)
    return config


def test_routing_recovers_exact_paid_endpoint_without_model_sibling_redirect(routes):
    recover = getattr(custom, "recover_custom_route_identity", None)
    assert recover is not None, "Missing routing-only identity recovery"
    assert recover(
        base_url=" HTTPS://GATEWAY.EXAMPLE/v1/ ", api_mode="responses", model="gpt6",
    ) == "custom:custom-codex_responses"
    # Returning an identity did not opt the old authorization helper into paid tiers.
    assert rp.canonical_custom_identity(base_url="https://gateway.example/v1", model="gpt6") is None


def test_strict_canonical_does_not_redirect_paid_exact_endpoint_to_model_sibling(routes):
    assert rp.canonical_custom_identity(
        base_url="https://gateway.example/v1", model="gpt6",
    ) is None


@pytest.mark.parametrize("declaration", [
    {"service_tiers": ["priority"]},
    {"service_tiers": []},
    {"models": {"gpt6": {"service_tiers": []}}},
    {"models": [{"id": "gpt6", "service_tiers": ["priority"]}]},
])
def test_raw_tier_declarations_recover_route_but_not_paid_authority(routes, declaration):
    routes["providers"]["custom-codex_responses"].update(declaration)
    assert custom.recover_custom_route_identity(base_url="https://gateway.example/v1") == "custom:custom-codex_responses"
    assert rp.canonical_custom_identity(
        base_url="https://gateway.example/v1", config_provider="custom-anthropic_messages",
    ) is None


def test_protocol_disambiguates_same_full_endpoint_without_model_guess(routes):
    routes["providers"]["custom-anthropic_messages"]["base_url"] = "https://gateway.example/v1/"
    assert custom.recover_custom_route_identity(base_url="https://gateway.example/v1", model="gpt6") is None
    assert custom.recover_custom_route_identity(
        base_url="https://gateway.example/v1", api_mode=" OPENAI-RESPONSES ",
    ) == "custom:custom-codex_responses"
    assert custom.recover_custom_route_identity(
        base_url="https://gateway.example/v1", api_mode="messages",
    ) == "custom:custom-anthropic_messages"
    assert rp.canonical_custom_identity(base_url="https://gateway.example/v1", model="gpt6") is None


def test_same_url_same_protocol_remains_ambiguous_even_with_unique_model(routes):
    routes["providers"]["duplicate"] = {
        "base_url": "https://gateway.example/v1", "transport": "responses", "models": {"other": {}},
    }
    assert custom.recover_custom_route_identity(
        base_url="https://gateway.example/v1", api_mode="codex_responses", model="gpt6",
    ) is None


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions", "not_a_protocol", ""])
def test_protocol_mismatch_cannot_redirect_to_same_model_sibling(routes, mode):
    assert custom.recover_custom_route_identity(
        base_url="https://gateway.example/v1", api_mode=mode, model="gpt6",
    ) is None


@pytest.mark.parametrize("flag", [False, "false", "off", 0])
def test_disabled_raw_entries_are_not_recovered_or_counted_as_ambiguity(routes, flag):
    routes["providers"]["disabled"] = {
        "base_url": "https://gateway.example/v1", "api_mode": "responses", "enabled": flag,
    }
    assert custom.recover_custom_route_identity(base_url="https://gateway.example/v1") == "custom:custom-codex_responses"
    routes["providers"]["custom-codex_responses"]["enabled"] = flag
    assert custom.recover_custom_route_identity(base_url="https://gateway.example/v1", model="gpt6") is None


@pytest.mark.parametrize("base_url", ["https://unknown.example/v1", "https://gateway.example/v2", "", None])
def test_unknown_endpoint_never_uses_model_or_configured_default(routes, base_url):
    assert custom.recover_custom_route_identity(base_url=base_url, model="gpt6") is None


@pytest.mark.parametrize("provider_key, expected", [("", "custom:legacy-paid"), ("legacy-key", "custom:legacy-key")])
def test_enabled_raw_legacy_entry_retains_tiers_and_stable_identity(routes, provider_key, expected):
    routes["custom_providers"] = [{
        "name": "Legacy Paid", "provider_key": provider_key, "base_url": "https://legacy.example/v1/",
        "api_mode": "responses", "service_tiers": [], "models": {"gpt6": {}},
    }]
    assert custom.recover_custom_route_identity(
        base_url="https://legacy.example/v1", api_mode="codex_responses", model="gpt6",
    ) == expected
    assert rp.canonical_custom_identity(base_url="https://legacy.example/v1", model="gpt6") is None
    routes["custom_providers"][0]["enabled"] = False
    assert custom.recover_custom_route_identity(base_url="https://legacy.example/v1") is None


def test_legacy_and_new_style_exact_matches_are_ambiguous(routes):
    routes["custom_providers"] = [{
        "name": "legacy", "base_url": "https://gateway.example/v1", "api_mode": "codex_responses",
    }]
    assert custom.recover_custom_route_identity(
        base_url="https://gateway.example/v1", api_mode="responses",
    ) is None


def test_unique_unpaid_canonical_route_is_unchanged(routes):
    assert rp.canonical_custom_identity(base_url="https://gateway.example", model="gpt6") == "custom:custom-anthropic_messages"


def test_ambiguous_unpaid_canonical_route_refuses_model_and_provider_redirect(routes):
    routes["providers"]["duplicate"] = {"base_url": "https://gateway.example", "models": {"unrelated": {}}}
    assert rp.canonical_custom_identity(
        base_url="https://gateway.example", model="gpt6", config_provider="custom-anthropic_messages",
    ) is None


def test_routing_does_not_inspect_credentials_or_project_raw_entries(routes, monkeypatch):
    class MetadataOnly(dict):
        def get(self, key, default=None):
            assert key not in {"api_key", "key_env", "api_key_env", "key_cmd", "extra_headers"}
            return super().get(key, default)

    routes["providers"]["custom-codex_responses"] = MetadataOnly(routes["providers"]["custom-codex_responses"])
    monkeypatch.setattr(rp, "get_compatible_custom_providers", lambda *args: pytest.fail("Projected view loses raw metadata"))
    monkeypatch.setattr(rp, "_get_named_custom_provider", lambda *args: pytest.fail("Named runtime can resolve credentials"))
    assert custom.recover_custom_route_identity(
        base_url="https://gateway.example/v1", api_mode="responses",
    ) == "custom:custom-codex_responses"


@pytest.mark.parametrize("configured", [
    "https://gateway.example/TenantA/v1",
    "https://gateway.example/v1?tenant=TenantA",
    "https://TenantA@gateway.example/v1",
])
def test_resume_never_sends_named_credential_to_case_distinct_route(monkeypatch, configured):
    import json
    from tui_gateway import server

    different = configured.replace("TenantA", "tenanta")
    config = {"model": {"provider": "entry-a", "default": "synthetic-model"}, "providers": {
        "entry-a": {"base_url": configured, "transport": "codex_responses",
                    "service_tiers": ["priority"], "api_key": "synthetic-route-key"},
    }}
    monkeypatch.setattr(rp, "load_config", lambda: config)
    monkeypatch.setattr(rp, "_get_model_config", lambda: config["model"])
    monkeypatch.setattr(rp, "_try_resolve_from_custom_pool", lambda *args, **kw: None)
    monkeypatch.setattr("socket.socket.connect", lambda *args, **kw: pytest.fail("Network forbidden"))

    def runtime_for(url):
        row = {"model": "synthetic-model", "billing_provider": "custom", "billing_base_url": url,
               "model_config": json.dumps({"api_mode": "codex_responses"})}
        overrides = server._stored_session_runtime_overrides(row)
        return server._resolve_agent_model_runtime(overrides["model_override"], overrides.get("provider_override"))[1]

    exact = runtime_for(configured)
    assert exact["api_key"] == "synthetic-route-key"
    assert exact["requested_provider"] == "custom:entry-a"
    assert exact["base_url"] == configured
    different_runtime = runtime_for(different)
    assert not (different_runtime["api_key"] == "synthetic-route-key" and different_runtime["base_url"] == different)
    assert custom.recover_custom_route_identity(base_url=different, api_mode="codex_responses") is None


def test_missing_config_fails_closed(routes, monkeypatch):
    def unavailable():
        raise OSError("isolated config unavailable")
    monkeypatch.setattr(rp, "load_config", unavailable)
    assert custom.recover_custom_route_identity(base_url="https://gateway.example/v1") is None


def test_route_config_failure_logs_no_exception_payload(monkeypatch, caplog):
    import logging
    import hermes_yaml as yaml

    canary = "SYNTHETIC_ONLY_KEY"

    def malformed_config():
        # YAML parse errors include source lines, which may contain credentials.
        return yaml.safe_load("providers: [\napi_key: " + canary)

    monkeypatch.setattr(rp, "load_config", malformed_config)
    with caplog.at_level(logging.DEBUG, logger=custom.logger.name):
        assert custom.recover_custom_route_identity(base_url="https://gateway.example/v1") is None
    assert caplog.records, "Configuration failure should retain a fixed diagnostic"
    assert canary not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
