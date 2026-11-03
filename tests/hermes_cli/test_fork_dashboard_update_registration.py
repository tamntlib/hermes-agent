"""Dashboard update registration without spawning a process or contacting a service."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import web_server_gateway as gateway


@pytest.mark.platforms("linux")
def test_update_spawn_registers_pid_starttime_owner_and_cgroup(tmp_path, monkeypatch):
    from hermes_cli import _launchers, web_server
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(gateway, "_ACTION_LOG_DIR", tmp_path / "logs")
    for attribute in ("_ACTION_PROCS", "_ACTION_COMMANDS", "_ACTION_IDS", "_ACTION_RESULTS"):
        monkeypatch.setattr(gateway, attribute, {})
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_launchers, "runtime_command", lambda root, args: ["synthetic-hermes", *args])
    monkeypatch.setattr(gateway, "_action_targets_system_gateway", lambda args: False)
    monkeypatch.setattr(gateway, "_profile_action_environment", lambda *a: {})
    process = Mock(pid=43210)
    popen = Mock(return_value=process)
    monkeypatch.setattr(gateway.subprocess, "Popen", popen)
    original_read = Path.read_text

    def read_proc(path, *args, **kwargs):
        if str(path) in ("/proc/43210/stat", f"/proc/{os.getpid()}/stat"):
            # stat fields 3..22: state, ppid, pgrp, session, ..., starttime.
            fields = ["S", "1", "2", "678"] + ["0"] * 15 + ["987654"]
            return f"{path.parent.name} (synthetic name with ) parens) " + " ".join(fields)
        if str(path) == "/proc/self/cgroup":
            return "0::/synthetic.slice/dashboard.service\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_proc)
    assert gateway._spawn_hermes_action(["update"], "hermes-update") is process
    record_path = runtime / "hermes-dashboard-updater.json"
    assert record_path.exists(), "dashboard update must publish identity for the external cleanup helper"
    record = json.loads(record_path.read_text())
    assert record == {"pid": 43210, "sid": 678, "starttime": 987654,
                      "owner": {"pid": os.getpid(), "sid": 678, "starttime": 987654},
                      "cgroup": "0::/synthetic.slice/dashboard.service"}
    assert record_path.stat().st_mode & 0o777 == 0o600
    assert list(runtime.iterdir()) == [record_path]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert gateway._ACTION_PROCS["hermes-update"] is process

    # Other dashboard actions do not overwrite an outstanding updater identity.
    record_path.write_text("sentinel")
    gateway._spawn_hermes_action(["doctor"], "doctor")
    assert record_path.read_text() == "sentinel"


@pytest.mark.platforms("linux")
def test_missing_runtime_registration_is_best_effort(monkeypatch, capsys):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert gateway._record_dashboard_updater(os.getpid()) is None
    assert "updater identity unavailable" in capsys.readouterr().err


@pytest.mark.platforms("linux")
def test_failed_atomic_publication_removes_temporary_file(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    replace = Mock(side_effect=OSError("synthetic publication failure"))
    monkeypatch.setattr(os, "replace", replace)
    assert gateway._record_dashboard_updater(os.getpid()) is None
    replace.assert_called_once()
    assert list(runtime.iterdir()) == []
    assert "synthetic publication failure" in capsys.readouterr().err
