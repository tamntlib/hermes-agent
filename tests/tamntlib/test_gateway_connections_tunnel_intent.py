"""Shared-store writers keep Desktop-only URL-derived SSH tunnel intent, never dial it directly."""
from tests.hermes_cli.conftest import (
    _suppress_concurrent_hermes_gate,
    _private_repo_checkout_lock,
    _source_channels_resolve_locally,
    _no_detached_commit_graph_writer,
    _discharge_host_update_obligation,
    _reset_prompt_toolkit_output_cache,
)

import json

from copy import deepcopy

import pytest

from hermes_cli import gateway_connections as store
from hermes_cli.gateway_connections_normalize import normalize_registry


def tunneled(**changes):
    return {"id": "remote", "kind": "remote", "label": "Remote", "url": "http://backend:9119",
            "token": "synthetic", "sshTunnel": {"host": "ssh.example", "port": 2222, "remotePort": 99}, **changes}


def test_shared_round_trip_keeps_login_separate_and_backend_port_url_authoritative(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.store_path().write_text(json.dumps({"version": 1, "connections": [tunneled()]}))
    store.set_launch_mode("last-used")
    connection = store.read_registry()["connections"][1]
    assert connection["sshTunnel"] == {"host": "ssh.example", "port": 2222, "remotePort": 9119}
    store.save_connection({"id": "remote", "kind": "remote", "label": "Renamed", "url": "https://backend"})
    assert store.read_registry()["connections"][1]["sshTunnel"]["remotePort"] == 443
    store.save_connection({"id": "remote", "kind": "remote", "label": "Renamed", "sshTunnel": None})
    assert "sshTunnel" not in store.read_registry()["connections"][1]


@pytest.mark.parametrize("url,port", [("http://backend", 80), ("https://backend", 443), ("https://backend:9443", 9443)])
def test_shared_normalization_uses_url_defaults_not_legacy_port(url, port):
    normalized = normalize_registry({"connections": [tunneled(url=url)]})["connections"][1]
    assert normalized["sshTunnel"]["remotePort"] == port


@pytest.mark.parametrize("entry", [tunneled(), tunneled(sshTunnel={"host": "-unsafe"}),
                                    tunneled(sshTunnel=None, sshTunnelError="stored tunnel error")])
def test_shared_dial_refuses_tunnel_intent_before_headers_or_transport(entry, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.store_path().write_text(json.dumps({"version": 1, "connections": [entry]}))
    monkeypatch.setattr(store, "_secret_of", lambda *_: pytest.fail("Must refuse before credential access"))
    with pytest.raises(store.ConnectionStoreError, match="Desktop"):
        store.dial_snapshot("remote")


@pytest.mark.parametrize("field", ["sshTunnel", "terminalSsh"])
@pytest.mark.parametrize("value", [False, "", 0, [], "invalid"])
@pytest.mark.parametrize("stored_error", [False, True])
def test_malformed_tunnel_survives_unrelated_writes_and_refuses_dial(
    field, value, stored_error, tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entry = tunneled(sshTunnel=None)
    entry[field] = value
    if stored_error:
        entry["sshTunnelError"] = "stored error"
    store.store_path().write_text(json.dumps({"connections": [entry], "primary": "remote", "lastUsed": "remote"}))
    store.set_launch_mode("last-used")
    registry = store.read_registry()
    saved = next(item for item in registry["connections"] if item["id"] == "remote")
    assert saved.get("sshTunnelError"), "Malformed non-null intent must not become a direct route"
    assert registry["primary"] == registry["lastUsed"] == "remote"
    monkeypatch.setattr(store, "_secret_of", lambda *_: pytest.fail("Must refuse before credential access"))
    with pytest.raises(store.ConnectionStoreError, match="Desktop"):
        store.dial_snapshot("remote")


@pytest.mark.parametrize("damage", [{"url": ""}, {"url": None}, {"kind": "corrupt"},
                                    {"kind": "ssh", "host": ""}, {"kind": "cloud", "url": ""}])
@pytest.mark.parametrize("intent", [{"sshTunnel": {"host": "ssh.example"}},
                                    {"sshTunnel": None, "terminalSsh": False},
                                    {"sshTunnel": None, "sshTunnelError": "stored error"}])
def test_invalid_connection_retains_blocked_selected_identity(damage, intent, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw = tunneled(**{**damage, **intent})
    store.store_path().write_text(json.dumps({"connections": [raw], "primary": "remote", "lastUsed": "remote"}))
    for mode in ["last-used", "primary"]:
        store.set_launch_mode(mode)
        registry = store.read_registry()
        assert registry["primary"] == registry["lastUsed"] == "remote", "Corrupt SSH selection must not become Local"
        saved = next(item for item in registry["connections"] if item["id"] == "remote")
        assert saved.get("sshTunnelError")
        monkeypatch.setattr(store, "_secret_of", lambda *_: pytest.fail("Must refuse before credential access"))
        with pytest.raises(store.ConnectionStoreError, match="Desktop"):
            store.dial_snapshot("remote")


def test_legacy_tunnel_alias_is_preserved_and_dial_changes_are_detected():
    from hermes_cli.gateway_connections_normalize import connection_dial_fields_changed

    raw = tunneled()
    raw["terminalSsh"] = raw.pop("sshTunnel")
    normalized = normalize_registry({"connections": [raw]})["connections"][1]
    assert normalized["sshTunnel"]["remotePort"] == 9119
    renamed = {**normalized, "label": "Renamed"}
    assert not connection_dial_fields_changed(normalized, renamed)
    changed = {**normalized, "sshTunnel": {**normalized["sshTunnel"], "host": "new-ssh.example"}}
    assert connection_dial_fields_changed(normalized, changed)


def test_invalid_tunnel_is_quarantined_without_losing_blocked_entry():
    raw = tunneled(sshTunnel={"host": "-unsafe"})
    normalized = normalize_registry({"connections": [raw]})
    assert normalized["quarantined"] == [{"reason": "entry-invalid-ssh-tunnel", "entry": raw}]
    assert normalized["connections"][1]["sshTunnelError"] == "Invalid SSH tunnel configuration. Update it in Connections."


@pytest.mark.parametrize("url", ["http://backend:0", "http://backend:99999", "ftp://backend:22"])
def test_invalid_gateway_url_keeps_intent_as_an_error(url):
    normalized = normalize_registry({"connections": [tunneled(url=url)]})["connections"][1]
    assert "sshTunnel" not in normalized
    assert normalized["sshTunnelError"]
    assert "sshTunnelError" in normalize_registry({"connections": [deepcopy(normalized)]})["connections"][1]
