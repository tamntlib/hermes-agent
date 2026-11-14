"""Source update checks and checkout moves belong to origin, never upstream."""

from __future__ import annotations

from tests.hermes_cli.conftest import (
    _suppress_concurrent_hermes_gate,
    _private_repo_checkout_lock,
    _source_channels_resolve_locally,
    _no_detached_commit_graph_writer,
    _discharge_host_update_obligation,
    _reset_prompt_toolkit_output_cache,
)

import os
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import source_check, update_cmd, update_cmd_check


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def fork(tmp_path, monkeypatch):
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "base.txt").write_text("base\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "base")
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "remote", "add", "upstream", str(upstream))
    # Only upstream advances; origin and HEAD deliberately remain at the base.
    (upstream / "upstream-only.txt").write_text("not approved\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "upstream-only")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", clone)
    return SimpleNamespace(clone=clone, origin=origin, upstream=upstream)


def test_cli_check_uses_origin_even_when_upstream_is_newer(fork, capsys):
    head = _git(fork.clone, "rev-parse", "HEAD")
    update_cmd._cmd_update_check(branch="main", branch_explicit=True)
    output = capsys.readouterr().out
    assert "Already up to date" in output, output
    assert "Fetching from origin" in output
    assert _git(fork.clone, "rev-parse", "HEAD") == head
    assert _git(fork.clone, "for-each-ref", "--format=%(refname)", "refs/remotes/upstream") == ""
    assert _git(fork.origin, "rev-parse", "main") == head


@pytest.mark.parametrize("has_upstream", [True, False])
def test_noop_update_does_not_sync_or_offer_upstream(fork, monkeypatch, has_upstream):
    if not has_upstream:
        _git(fork.clone, "remote", "remove", "upstream")
    head = _git(fork.clone, "rev-parse", "HEAD")
    remotes = _git(fork.clone, "remote", "-v")
    upstream_tip = _git(fork.upstream, "rev-parse", "HEAD")
    prompts = []

    def prompt(*args):
        prompts.append(args)
        return "n"

    plan = update_cmd._prepare_checkout_for_update(
        ["git"], "main", "main", is_fork=True, assume_yes=False, gateway_mode=True,
        gw_input_fn=prompt, switch_branch=False, _windows_gateway_resume=None,
    )
    assert plan.commit_count == 0
    assert prompts == []
    assert _git(fork.clone, "rev-parse", "HEAD") == head
    assert _git(fork.clone, "remote", "-v") == remotes
    assert _git(fork.clone, "for-each-ref", "--format=%(refname)", "refs/remotes/upstream") == ""
    assert _git(fork.origin, "rev-parse", "main") == head
    assert _git(fork.upstream, "rev-parse", "HEAD") == upstream_tip


def test_apply_lands_only_origin_without_publishing_fork(fork):
    # Origin approves c1, while upstream is already at the unapproved c2.
    _git(fork.origin, "fetch", "-q", str(fork.upstream), "main")
    _git(fork.origin, "update-ref", "refs/heads/main", "FETCH_HEAD")
    approved = _git(fork.origin, "rev-parse", "main")
    (fork.upstream / "newer.txt").write_text("later upstream change\n", encoding="utf-8")
    _git(fork.upstream, "add", ".")
    _git(fork.upstream, "commit", "-qm", "not yet approved")
    upstream_tip = _git(fork.upstream, "rev-parse", "HEAD")
    _git(fork.clone, "fetch", "-q", "origin", "main")

    update_cmd._pull_updates(
        ["git"], "main", None, prompt_for_restore=False, gw_input_fn=None,
        discard_local_changes=False, keep_stash=False, sync_upstream=True, assume_yes=True,
    )
    assert _git(fork.clone, "rev-parse", "HEAD") == approved
    assert _git(fork.origin, "rev-parse", "main") == approved
    assert _git(fork.upstream, "rev-parse", "HEAD") == upstream_tip
    assert _git(fork.clone, "for-each-ref", "--format=%(refname)", "refs/remotes/upstream") == ""


def _shallow_github_fork(fork, monkeypatch, origin_url):
    clone = fork.clone.parent / "shallow"
    _git(fork.clone.parent, "clone", "-q", "--depth", "1", fork.origin.as_uri(), str(clone))
    (fork.clone / "fork-only.txt").write_text("fork approval\n", encoding="utf-8")
    _git(fork.clone, "add", ".")
    _git(fork.clone, "commit", "-qm", "only on fork")
    _git(fork.origin, "fetch", "-q", str(fork.clone), "main")
    _git(fork.origin, "update-ref", "refs/heads/main", "FETCH_HEAD")
    # Git fetch stays local; source_repository must use the configured GitHub origin.
    _git(clone, "remote", "set-url", "origin", origin_url)
    _git(clone, "config", f"url.{fork.origin.as_uri()}.insteadOf", origin_url)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", clone)
    return clone


@pytest.mark.parametrize("origin_url", [
    "https://github.com/fork-owner/source.git",
    "git@github.com:fork-owner/source.git",
    "ssh://git@github.com/fork-owner/source.git",
])
@pytest.mark.parametrize("available", [True, False])
def test_shallow_check_compares_in_origin_github_repository(fork, monkeypatch, capsys, origin_url, available):
    clone = _shallow_github_fork(fork, monkeypatch, origin_url)
    urls = []

    def request(url, *args):
        urls.append(url)
        if not available:
            raise OSError("compare unavailable")
        return '{"ahead_by": 7}'

    monkeypatch.setattr(source_check, "_request", request)
    update_cmd._cmd_update_check(branch="main", branch_explicit=True)
    head, target = _git(clone, "rev-parse", "HEAD", "origin/main").splitlines()
    assert head != target
    assert urls == [f"https://api.github.com/repos/fork-owner/source/compare/{head}...{target}"]
    output = capsys.readouterr().out
    assert "7 commits behind origin/main" in output if available else "behind origin/main" in output


@pytest.mark.parametrize("available", [True, False])
def test_shallow_apply_compares_in_origin_github_repository(fork, monkeypatch, available):
    clone = _shallow_github_fork(fork, monkeypatch, "git@github.com:fork-owner/source.git")
    _git(clone, "fetch", "-q", "--depth", "1", "origin", "main")
    urls = []

    def request(url, *args):
        urls.append(url)
        if not available:
            raise OSError("compare unavailable")
        return '{"ahead_by": 7}'

    monkeypatch.setattr(source_check, "_request", request)
    plan = update_cmd._prepare_checkout_for_update(
        ["git"], "main", "main", is_fork=True, assume_yes=True, gateway_mode=False,
        gw_input_fn=None, switch_branch=False, _windows_gateway_resume=None,
    )
    head, target = _git(clone, "rev-parse", "HEAD", "origin/main").splitlines()
    assert head != target
    assert urls == [f"https://api.github.com/repos/fork-owner/source/compare/{head}...{target}"]
    assert plan.commit_count == (7 if available else -1)


def test_explicit_branch_keeps_origin_repository_for_zip_fallback(fork, monkeypatch):
    _git(fork.clone, "remote", "set-url", "origin", "git@github.com:fork-owner/source.git")
    args = SimpleNamespace(branch="main")
    target_ref, release_sha, target_is_head, repository = update_cmd_check.select_apply_target(
        args, "main", {}, git_cmd=["git"], stop=lambda: pytest.fail("unexpected refusal"),
    )
    assert (target_ref, release_sha, target_is_head) == ("origin/main", None, False)
    assert repository == "fork-owner/source"
    fallback = []
    monkeypatch.setattr(update_cmd, "_should_zip_fallback_on_update_error", lambda error: True)
    monkeypatch.setattr(update_cmd, "_update_via_zip", lambda *a, **kw: fallback.append(kw))
    update_cmd._handle_update_called_process_error(
        subprocess.CalledProcessError(128, ["git", "fetch"]), args, False, False,
        target_repository=repository, completion_request={},
    )
    assert fallback[0]["target_repository"] == "fork-owner/source"
