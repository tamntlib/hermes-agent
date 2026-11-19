"""Preserve Desktop-only HTTP-over-SSH intent in the machine-wide gateway store."""
import re
from urllib.parse import urlsplit


def _validate_tunnel_shape(raw):
    if not isinstance(raw, dict) or set(raw) - {"host", "user", "port", "keyPath", "remotePort"}:
        raise ValueError("Invalid SSH tunnel configuration.")
    if not isinstance(raw.get("host"), str) or any(
        raw.get(field) is not None and not isinstance(raw[field], str) for field in ("user", "keyPath")
    ):
        raise ValueError("Invalid SSH tunnel configuration.")


def _validate_tunnel_ports(raw):
    """Validate both raw ports before precedence/defaulting, as Desktop does."""
    from hermes_cli.gateway_connections_normalize import js_string

    embedded = re.search(r"(?:\]|^[^:]+):(\d+)$", raw["host"].strip())
    ports = [embedded.group(1)] if embedded else []
    if raw.get("port") is not None:
        ports.append(js_string(raw["port"]))
    for port in ports:
        if not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid SSH login port.")


def normalize_tunnel(raw, gateway_url):
    """Return the Desktop tunnel shape; the Gateway URL alone supplies its backend port."""
    from hermes_cli.gateway_connections_normalize import js_str, normalize_ssh_config
    from hermes_cli.gateway_connections_ssh_transport import validate_key_path, validate_ssh_target

    if raw is None:
        return None
    _validate_tunnel_shape(raw)
    _validate_tunnel_ports(raw)
    ssh = normalize_ssh_config({**raw, "mode": "ssh"})
    if not ssh:
        raise ValueError("SSH host is required.")
    validate_ssh_target(ssh["host"], ssh.get("user", ""), ssh.get("port", 22))
    validate_key_path(ssh.get("keyPath", ""))
    try:
        url = urlsplit(js_str(gateway_url).strip())
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("Invalid Gateway URL.")
        remote_port = url.port
        if remote_port is not None and not 1 <= remote_port <= 65535:
            raise ValueError("Invalid Gateway port.")
    except ValueError as exc:
        raise ValueError("SSH tunnel requires a valid HTTP(S) Gateway URL and port.") from exc
    return {"remotePort": remote_port or (443 if url.scheme == "https" else 80),
            **{field: ssh[field] for field in ("host", "user", "port", "keyPath") if field in ssh}}


def copy_tunnel_intent(clean, entry):
    """Malformed or error-only intent must remain fail-closed after another writer saves the list."""
    from hermes_cli.gateway_connections_normalize import js_falsy
    from hermes_cli.gateway_connections_ssh_transport import SshReachError

    raw = entry.get("sshTunnel")
    if raw is None:
        raw = entry.get("terminalSsh")
    error = any(not js_falsy(entry.get(field)) for field in ("sshTunnelError", "terminalSshError"))
    if raw is not None:
        try:
            if clean.get("kind") != "remote":
                raise ValueError("Invalid SSH tunnel connection kind.")
            tunnel = normalize_tunnel(raw, entry.get("url"))
        except (ValueError, SshReachError):
            clean["sshTunnelError"] = "Invalid SSH tunnel configuration. Update it in Connections."
        else:
            if tunnel:
                clean["sshTunnel"] = tunnel
    elif error:
        clean["sshTunnelError"] = "Invalid SSH tunnel configuration. Update it in Connections."


def preserve_blocked_tunnel_entries(connections, raw_connections):
    """A quarantined SSH selection stays addressable; corrupt data must never select Local."""
    from hermes_cli.gateway_connections_normalize import js_falsy, js_str, unique_label

    for entry in raw_connections if isinstance(raw_connections, list) else []:
        if not isinstance(entry, dict):
            continue
        intent = (entry.get("sshTunnel") is not None or entry.get("terminalSsh") is not None
                  or not js_falsy(entry.get("sshTunnelError")) or not js_falsy(entry.get("terminalSshError")))
        cid = entry.get("id")
        if not intent or not isinstance(cid, str) or not cid.strip():
            continue
        cid = cid.strip()
        if any(connection["id"] == cid for connection in connections):
            continue
        blocked = {"id": cid, "kind": "remote",
                   "label": unique_label(js_str(entry.get("label")) or "Invalid SSH Gateway",
                                         [connection["label"] for connection in connections]),
                   "sshTunnelError": "Invalid SSH tunnel connection. Update it in Connections."}
        if isinstance(entry.get("url"), str):
            blocked["url"] = entry["url"]
        connections.append(blocked)
