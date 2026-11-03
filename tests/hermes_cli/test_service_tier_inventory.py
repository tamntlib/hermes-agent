"""Custom-provider tier declarations must drive the existing inventory picker."""
from hermes_cli.inventory import _apply_capabilities


def test_custom_ultrafast_is_independent_of_priority():
    config = {"providers": {"cpa": {
        "base_url": "https://cpa.example/v1", "transport": "codex_responses",
        "service_tiers": ["ultrafast"],
    }}}
    rows = [{"slug": "cpa", "is_user_defined": True, "api_url": "https://cpa.example/v1", "models": ["alias-sol"]}]
    _apply_capabilities(rows, metadata_config=config)
    caps = rows[0]["capabilities"]["alias-sol"]
    assert caps["fast"] is False
    assert caps.get("ultrafast") is True


def test_builtin_openai_settings_without_declaration_keep_builtin_fast():
    config = {"providers": {"openai": {
        "base_url": "https://api.openai.com/v1", "transport": "codex_responses",
    }}}
    rows = [{"slug": "openai", "is_user_defined": False,
             "api_url": "https://api.openai.com/v1", "models": ["gpt-5.4"]}]

    _apply_capabilities(rows, metadata_config=config)

    assert rows[0]["capabilities"]["gpt-5.4"]["fast"] is True


def test_legacy_custom_slug_uses_exact_name_not_shared_endpoint():
    endpoint = "https://cpa.example/v1"
    config = {"custom_providers": [
        {"name": "cpa", "base_url": endpoint, "api_mode": "codex_responses",
         "service_tiers": ["ultrafast"]},
        {"name": "sibling", "base_url": endpoint, "api_mode": "codex_responses"},
    ]}
    rows = [{"slug": slug, "is_user_defined": True, "api_url": endpoint,
             "models": ["alias-sol"]} for slug in ("custom:cpa", "custom:sibling")]

    _apply_capabilities(rows, metadata_config=config)

    assert rows[0]["capabilities"]["alias-sol"] == {
        "fast": False, "reasoning": True, "ultrafast": True,
    }
    assert rows[1]["capabilities"]["alias-sol"] == {"fast": False, "reasoning": True}


def test_builtin_identity_still_honors_explicit_tier_denial():
    config = {"providers": {"openai": {
        "base_url": "https://api.openai.com/v1", "transport": "codex_responses",
        "service_tiers": [],
    }}}
    rows = [{"slug": "openai", "is_user_defined": False,
             "api_url": "https://api.openai.com/v1", "models": ["gpt-5.4"]}]

    _apply_capabilities(rows, metadata_config=config)

    assert rows[0]["capabilities"]["gpt-5.4"]["fast"] is False
