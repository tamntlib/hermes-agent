"""Exercise the actual stable-sync workflow shell offline with mock gh and real jq."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote

import pytest

from hermes_yaml import safe_load


pytestmark = pytest.mark.platforms("linux", "macos")
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/sync-upstream.yml"
SHA = "a" * 40
REPO = "fixture-owner/hermes-fork"
UPSTREAM = "fixture-upstream/hermes-agent"

MOCK_GH = '''#!{python}
import json
import os
from pathlib import Path
import sys
case = json.loads(Path(os.environ["GH_MOCK_CASE"]).read_text())
log = Path(os.environ["GH_MOCK_LOG"])
calls = json.loads(log.read_text()) if log.exists() else []
args = sys.argv[1:]
calls.append(args)
log.write_text(json.dumps(calls))
index = len(calls) - 1
if index >= len(case) or args != case[index]["args"]:
    print("Unexpected gh call: " + repr(args), file=sys.stderr)
    sys.exit(91)
response = case[index]
print(response.get("stdout", ""), end="")
print(response.get("stderr", ""), end="", file=sys.stderr)
sys.exit(response.get("exit", 0))
'''


def response(args, stdout="", exit=0, stderr=""):
    return {"args": ["api", *args], "stdout": stdout, "exit": exit, "stderr": stderr}


def discovery(release):
    return [
        response([f"repos/{REPO}", "--jq", ".parent.full_name"], UPSTREAM),
        response([f"repos/{UPSTREAM}/releases/latest"], json.dumps(release)),
    ]


def stable_release(**changes):
    return {"tag_name": "v1.2.3", "draft": False, "prerelease": False,
            "target_commitish": "main", **changes}


def resolution(sha=SHA, tag="v1.2.3"):
    encoded = quote(str(tag), safe="")
    return response([f"repos/{UPSTREAM}/commits/{encoded}", "--jq", ".sha"], sha)


def merge(branch="main", status=201, exit=0, sha=SHA):
    return response(
        ["--include", "--method", "POST", f"repos/{REPO}/merges",
         "-f", f"base={branch}", "-f", f"head={sha}"],
        f"HTTP/2.0 {status} fixture\r\n\r\n", exit=exit,
        stderr=f"gh: fixture failure (HTTP {status})\n" if exit else "",
    )


@pytest.fixture
def run_sync(tmp_path):
    if not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("stable-sync shell fixtures require bash and jq")
    config = safe_load(WORKFLOW.read_text())
    steps = config["jobs"]["sync"]["steps"]
    shell = "\n".join(step["run"] for step in steps)
    executable = tmp_path / "gh"
    executable.write_text(MOCK_GH.format(python=sys.executable))
    executable.chmod(0o755)

    def run(responses, branch="main", ref_type="branch"):
        case = tmp_path / "case.json"
        log = tmp_path / "calls.json"
        case.write_text(json.dumps(responses))
        log.unlink(missing_ok=True)
        env = {
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "GH_TOKEN": "offline-fixture-not-a-credential",
            "GH_MOCK_CASE": str(case), "GH_MOCK_LOG": str(log),
            "GITHUB_REPOSITORY": REPO, "GITHUB_REF_NAME": branch,
            "GITHUB_REF_TYPE": ref_type, "TMPDIR": str(tmp_path),
        }
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", shell],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10, check=False,
        )
        calls = json.loads(log.read_text()) if log.exists() else []
        assert result.returncode != 91, result.stderr
        return result, calls

    return run


def test_prerelease_cannot_merge(run_sync):
    result, calls = run_sync(discovery(stable_release(prerelease=True)) + [resolution(), merge()])
    assert result.returncode != 0, "prerelease was merged"
    assert not any(f"repos/{REPO}/merges" in call for call in calls)


def test_stable_release_merges_resolved_tag_not_target_branch(run_sync):
    expected = discovery(stable_release()) + [resolution(), merge()]
    result, calls = run_sync(expected)
    assert result.returncode == 0, result.stderr
    assert calls == [item["args"] for item in expected]


def test_manual_tag_ref_cannot_mutate_a_same_named_branch(run_sync):
    result, calls = run_sync(discovery(stable_release()) + [resolution(), merge()], ref_type="tag")
    assert result.returncode != 0
    assert not calls


@pytest.mark.parametrize("sha", ["main", "", "null", "a" * 39])
def test_tag_lookup_must_return_a_full_commit_sha(run_sync, sha):
    expected = discovery(stable_release()) + [resolution(sha=sha)]
    result, calls = run_sync(expected + [merge(sha=sha)])
    assert result.returncode != 0, "non-SHA head reached merge API"
    assert calls == [item["args"] for item in expected]


def test_same_release_rerun_is_a_reported_noop(run_sync):
    expected = discovery(stable_release()) + [resolution(), merge(status=204)]
    for _ in range(2):
        result, calls = run_sync(expected)
        assert result.returncode == 0, result.stderr
        assert "Already up to date" in result.stdout
        assert calls == [item["args"] for item in expected]


def test_unexpected_merge_success_status_fails_visibly(run_sync):
    result, _calls = run_sync(discovery(stable_release()) + [resolution(), merge(status=200)])
    assert result.returncode != 0
    assert "Unexpected merge status" in result.stderr


@pytest.mark.parametrize("tag", [
    "canary", "v1.2.3+canary.20261102", "v1.2.3-rc.1", "rc.1-v1.2.3",
    "1.2.3", "v1.2", "v01.2.3", "v1.2.3/other", "v1.2.3?other#fragment",
    "v1.2.3$(touch injected)", "v1.2.3\nother", "v1.2.3\n", "", 123,
])
def test_nonstable_or_unsafe_tag_never_resolves_or_merges(run_sync, tmp_path, tag):
    expected = discovery(stable_release(tag_name=tag))
    result, calls = run_sync(expected + [resolution(tag=tag), merge()])
    assert not (tmp_path / "injected").exists()
    assert result.returncode != 0, f"invalid tag accepted: {tag!r}"
    assert calls == [item["args"] for item in expected]


@pytest.mark.parametrize("branch", ["stable/custom", "topic-$(touch${IFS}injected)"])
def test_manual_selected_branch_is_literal_merge_destination(run_sync, tmp_path, branch):
    expected = discovery(stable_release()) + [resolution(), merge(branch=branch)]
    result, calls = run_sync(expected, branch=branch)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "injected").exists()
    assert calls == [item["args"] for item in expected]


@pytest.mark.parametrize("changes", [
    {"draft": True}, {"draft": None}, {"prerelease": None}, {"tag_name": None},
])
def test_release_requires_explicit_published_stable_metadata(run_sync, changes):
    expected = discovery(stable_release(**changes))
    result, calls = run_sync(expected)
    assert result.returncode != 0
    assert calls == [item["args"] for item in expected]


@pytest.mark.parametrize("stage,status", [
    (0, 401), (1, 404), (1, 500), (2, 404), (2, 403),
    (3, 409), (3, 401), (3, 403), (3, 422), (3, 500),
])
def test_api_failures_stop_immediately_and_remain_visible(run_sync, stage, status):
    expected = discovery(stable_release()) + [resolution(), merge()]
    expected[stage].update(exit=1, stdout="", stderr=f"gh: fixture failure (HTTP {status})\n")
    result, calls = run_sync(expected)
    assert result.returncode != 0
    assert f"HTTP {status}" in result.stderr
    assert calls == [item["args"] for item in expected[:stage + 1]]
