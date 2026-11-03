"""Legacy URL/model healing is not an authorization source for paid tiers."""
import pytest

from hermes_cli import runtime_provider as rp


@pytest.mark.parametrize('declaration', [
    {'service_tiers': ['priority']},
    {'models': {'alias': {'service_tiers': ['priority']}}},
])
def test_legacy_healing_cannot_authorize_an_opted_in_provider(monkeypatch, declaration):
    config = {'model': {'provider': 'cpa'}, 'providers': {'cpa': {
        'base_url': 'https://cpa.example/v1', 'default_model': 'alias', **declaration,
    }}}
    monkeypatch.setattr(rp, 'load_config', lambda: config)
    monkeypatch.setattr(rp, '_get_model_config', lambda: config['model'])
    assert rp.canonical_custom_identity(base_url='https://cpa.example/v1', model='alias') is None


def test_canonical_config_failure_logs_no_exception_payload(monkeypatch, caplog):
    import logging
    import hermes_yaml as yaml

    canary = "SYNTHETIC_ONLY_KEY"

    def malformed_config():
        return yaml.safe_load("providers: [\napi_key: " + canary)

    monkeypatch.setattr(rp, "load_config", malformed_config)
    with caplog.at_level(logging.DEBUG, logger="hermes_cli.runtime_provider"):
        assert rp.canonical_custom_identity(base_url="https://gateway.example/v1") is None
    assert caplog.records
    assert canary not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("style", ["providers", "custom_providers"])
@pytest.mark.parametrize("configured,different", [
    ("https://gateway.example/TenantA/v1", "https://gateway.example/tenanta/v1"),
    ("https://gateway.example/v1?tenant=TenantA", "https://gateway.example/v1?tenant=tenanta"),
    ("https://TenantA@gateway.example/v1", "https://tenanta@gateway.example/v1"),
])
def test_identity_helpers_preserve_case_sensitive_url_components(monkeypatch, style, configured, different):
    entry = {"name": "entry-a", "base_url": configured, "api_mode": "codex_responses"}
    config = {"model": {"provider": "custom"},
              style: {"entry-a": entry} if style == "providers" else [entry]}
    monkeypatch.setattr(rp, "load_config", lambda: config)
    monkeypatch.setattr(rp, "_get_model_config", lambda: config["model"])
    for lookup in (rp.find_custom_provider_identity, rp.canonical_custom_identity):
        assert lookup(base_url=configured) == "custom:entry-a"
        assert lookup(base_url=different) is None


def test_identity_helpers_accept_only_proven_host_scheme_and_slash_equivalence(monkeypatch):
    config = {"providers": {"entry-a": {"base_url": "https://gateway.example/TenantA/v1"}}}
    monkeypatch.setattr(rp, "load_config", lambda: config)
    for lookup in (rp.find_custom_provider_identity, rp.canonical_custom_identity):
        assert lookup(base_url=" HTTPS://GATEWAY.EXAMPLE:443/TenantA/v1/ ") == "custom:entry-a"
