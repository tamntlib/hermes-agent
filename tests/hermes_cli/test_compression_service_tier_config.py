"""Compression has an optional tier policy, without changing other auxiliary tasks."""

from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_compression_service_tier_is_an_explicit_unset_default():
    compression = DEFAULT_CONFIG["auxiliary"]["compression"]
    assert "service_tier" in compression
    assert compression["service_tier"] is None
    assert compression["extra_body"] == {}


def test_compression_tier_default_does_not_enable_other_auxiliary_tasks():
    for name, task in DEFAULT_CONFIG["auxiliary"].items():
        if name != "compression" and isinstance(task, dict):
            assert "service_tier" not in task, name
