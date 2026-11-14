"""Compression has an optional tier policy, without changing other auxiliary tasks."""

from tests.hermes_cli.conftest import (
    _suppress_concurrent_hermes_gate,
    _private_repo_checkout_lock,
    _source_channels_resolve_locally,
    _no_detached_commit_graph_writer,
    _discharge_host_update_obligation,
    _reset_prompt_toolkit_output_cache,
)

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
