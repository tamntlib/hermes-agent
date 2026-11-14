"""Deployment policy: autonomous writes preserve installed/user ownership boundaries."""
from tests.tools.conftest import (
    _no_host_browser_use_cli,
    _no_chromium_download,
    _no_shared_temp_socket_dirs,
    _no_host_bot_desktop_autostart,
    _materialize_mcp_sdk_symbols,
    _clear_web_result_cache,
)

import json
from pathlib import Path

import pytest

from tools import skill_manager_guards as guards
from tools import skill_manager_tool  # registers the real handler
from tools.registry import registry
from tools.skill_provenance import (
    BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin,
)


def content(name):
    return f"---\nname: {name}\ndescription: Use when testing ownership.\n---\n\n# Test\n\nOld lesson.\n"


@pytest.fixture
def policy_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("skills:\n  write_approval: false\n")
    guards._reset_background_review_read_marks()
    yield skills
    guards._reset_background_review_read_marks()


def dispatch(operation):
    raw = registry.dispatch("skill_manage", {"operations": [operation]})
    assert isinstance(raw, str)
    return json.loads(raw)


@pytest.mark.parametrize("source", ["user", "pinned", "bundled", "hub"])
@pytest.mark.parametrize("action", ["patch", "write_file", "remove_file"])
def test_background_writes_refuse_protected_roots(policy_home, source, action):
    root = policy_home / "guarded"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_text(content("guarded"))
    support = root / "references" / "guide.md"
    support.parent.mkdir()
    support.write_text("Old lesson.")
    record = {"created_by": "user" if source == "user" else "agent", "pinned": source == "pinned"}
    (policy_home / ".usage.json").write_text(json.dumps({"guarded": record}))
    if source == "bundled":
        (policy_home / ".bundled_manifest").write_text("guarded:synthetic-hash\n")
    if source == "hub":
        (policy_home / ".hub").mkdir()
        (policy_home / ".hub" / "lock.json").write_text(json.dumps({"installed": {"guarded": {}}}))
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        guards.mark_background_review_skill_read(skill)
        guards.mark_background_review_skill_read(support)
        op = {"action": action, "name": "guarded"}
        if action == "patch":
            op.update(old_string="Old lesson.", new_string="New lesson.")
        else:
            op["file_path"] = "references/guide.md"
            if action == "write_file":
                op["file_content"] = "New lesson."
        out = dispatch(op)
        assert out["success"] is False, out
        assert "Refusing background curator" in out["error"]
        assert skill.read_text() == content("guarded")
        assert support.read_text() == "Old lesson."
    finally:
        reset_current_write_origin(token)


@pytest.mark.parametrize("ownership", ["local", "missing", "corrupt", "orphan"])
def test_extension_creation_requires_verified_protected_root(policy_home, ownership):
    if ownership != "orphan":
        root = policy_home / "root-skill"
        root.mkdir()
        (root / "SKILL.md").write_text(content("root-skill"))
    if ownership == "local":
        (policy_home / ".usage.json").write_text(json.dumps({"root-skill": {"created_by": "agent"}}))
    elif ownership == "corrupt":
        (policy_home / ".hub").mkdir()
        (policy_home / ".hub" / "lock.json").write_text("not json")
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        out = dispatch({"action": "create", "name": "root-skill-extension",
                        "content": content("root-skill-extension")})
        assert out["success"] is False, out
        assert not (policy_home / "root-skill-extension").exists()
    finally:
        reset_current_write_origin(token)


def test_owned_root_still_requires_read_before_successful_patch(policy_home):
    root = policy_home / "owned"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_text(content("owned"))
    (policy_home / ".usage.json").write_text(json.dumps({"owned": {"created_by": "agent"}}))
    op = {"action": "patch", "name": "owned", "old_string": "Old lesson.", "new_string": "New lesson."}
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        assert dispatch(op)["success"] is False
        guards.mark_background_review_skill_read(skill)
        out = dispatch(op)
        assert out["success"] is True, out
        assert "New lesson." in skill.read_text()
    finally:
        reset_current_write_origin(token)


def test_protected_root_extension_allowed_but_extension_ownership_checked(policy_home):
    root = policy_home / "bundled-root"
    root.mkdir()
    (root / "SKILL.md").write_text(content("bundled-root"))
    (policy_home / ".bundled_manifest").write_text("bundled-root:synthetic-hash\n")
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        out = dispatch({"action": "create", "name": "bundled-root-extension",
                        "content": content("bundled-root-extension")})
        assert out["success"] is True, out
        extension = policy_home / "bundled-root-extension" / "SKILL.md"
        guards.mark_background_review_skill_read(extension)
        (policy_home / ".usage.json").write_text(json.dumps({"bundled-root-extension": {"created_by": "user"}}))
        out = dispatch({"action": "patch", "name": "bundled-root-extension",
                        "old_string": "Old lesson.", "new_string": "New lesson."})
        assert out["success"] is False
        assert "Old lesson." in extension.read_text()
    finally:
        reset_current_write_origin(token)


@pytest.mark.parametrize("source", ["bundled", "hub", "pinned", "user"])
@pytest.mark.parametrize("action", ["patch", "edit", "write_file", "remove_file", "delete"])
def test_direct_alias_mutation_checks_canonical_protection(policy_home, source, action):
    root = policy_home / "category" / "directory-alias"
    root.mkdir(parents=True)
    skill = root / "SKILL.md"
    skill.write_text(content("canonical-name"), encoding="utf-8")
    support = root / "references" / "guide.md"
    support.parent.mkdir()
    support.write_text("Old lesson.", encoding="utf-8")
    (policy_home / ".usage.json").write_text(json.dumps({
        "directory-alias": {"created_by": "agent"},
        "canonical-name": {"created_by": "user" if source == "user" else "agent", "pinned": source == "pinned"},
    }), encoding="utf-8")
    if source == "bundled":
        (policy_home / ".bundled_manifest").write_text("canonical-name:synthetic-hash\n", encoding="utf-8")
    if source == "hub":
        lock = policy_home / ".hub" / "lock.json"
        lock.parent.mkdir()
        lock.write_text(json.dumps({"installed": {"canonical-name": {}}}), encoding="utf-8")
    op = {"action": action, "name": "directory-alias"}
    if action == "patch":
        op.update(old_string="Old lesson.", new_string="Unauthorized overwrite.")
    elif action == "edit":
        op["content"] = content("canonical-name").replace("Old lesson.", "Unauthorized overwrite.")
    elif action == "delete":
        umbrella = policy_home / "umbrella"
        umbrella.mkdir()
        (umbrella / "SKILL.md").write_text(content("umbrella"), encoding="utf-8")
        op["absorbed_into"] = "umbrella"
    else:
        op["file_path"] = "references/guide.md"
        op["file_content"] = "Unauthorized overwrite."
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        guards.mark_background_review_skill_read(skill)
        guards.mark_background_review_skill_read(support)
        result = dispatch(op)
        assert result["success"] is False, result
        assert skill.read_text(encoding="utf-8") == content("canonical-name")
        assert support.read_text(encoding="utf-8") == "Old lesson."
    finally:
        reset_current_write_origin(token)


@pytest.mark.parametrize("metadata", [
    "hub-corrupt", "hub-list", "hub-installed-list", "hub-installed-null",
    "usage-missing", "usage-corrupt", "usage-list", "pin-string", "canonical-missing",
])
def test_direct_background_mutation_fails_closed_on_unverified_metadata(policy_home, metadata, caplog):
    root = policy_home / "directory-alias"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_text(content("canonical-name"), encoding="utf-8")
    usage = policy_home / ".usage.json"
    usage.write_text(json.dumps({"canonical-name": {"created_by": "agent"},
                                 "directory-alias": {"created_by": "agent"}}), encoding="utf-8")
    if metadata.startswith("hub-"):
        lock = policy_home / ".hub" / "lock.json"
        lock.parent.mkdir()
        lock.write_text({"hub-corrupt": "invalid SYNTHETIC_ONLY_KEY", "hub-list": "[]",
                         "hub-installed-list": '{"installed": []}',
                         "hub-installed-null": '{"installed": null}'}[metadata], encoding="utf-8")
    elif metadata == "usage-missing":
        usage.unlink()
    elif metadata == "usage-corrupt":
        usage.write_text("invalid SYNTHETIC_ONLY_KEY", encoding="utf-8")
    elif metadata == "usage-list":
        usage.write_text("[]", encoding="utf-8")
    elif metadata == "pin-string":
        usage.write_text(json.dumps({"canonical-name": {"created_by": "agent", "pinned": "false"},
                                     "directory-alias": {"created_by": "agent"}}), encoding="utf-8")
    else:
        skill.write_text(content("canonical-name").replace("name: canonical-name\n", ""), encoding="utf-8")
    before = skill.read_bytes()
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        guards.mark_background_review_skill_read(skill)
        result = dispatch({"action": "patch", "name": "directory-alias", "old_string": "Old lesson.",
                           "new_string": "Unauthorized overwrite."})
        assert result["success"] is False, result
        assert skill.read_bytes() == before
        assert "SYNTHETIC_ONLY_KEY" not in caplog.text
    finally:
        reset_current_write_origin(token)


def test_verified_owned_alias_uses_canonical_management_record(policy_home):
    root = policy_home / "directory-alias"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_text(content("canonical-name"), encoding="utf-8")
    (policy_home / ".usage.json").write_text(json.dumps({
        "canonical-name": {"created_by": "agent"}, "directory-alias": {"created_by": "user"},
    }), encoding="utf-8")
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        guards.mark_background_review_skill_read(skill)
        result = dispatch({"action": "patch", "name": "directory-alias", "old_string": "Old lesson.",
                           "new_string": "New lesson."})
        assert result["success"] is True, result
    finally:
        reset_current_write_origin(token)


def test_classification_uses_canonical_frontmatter_not_directory_alias(policy_home):
    from tools.local_skill_policy import classify_root
    root = policy_home / "category" / "alias"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(content("canonical"))
    (policy_home / ".usage.json").write_text(json.dumps({"canonical": {"created_by": "agent"}}))
    assert classify_root({"path": root}) == ("canonical", "root")
    (policy_home / ".bundled_manifest").write_text("canonical:synthetic-hash\n")
    assert classify_root({"path": root}) == ("canonical", "extension")


def test_foreground_user_directed_patch_remains_allowed(policy_home):
    root = policy_home / "user-owned"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_text(content("user-owned"))
    out = dispatch({"action": "patch", "name": "user-owned",
                    "old_string": "Old lesson.", "new_string": "New lesson."})
    assert out["success"] is True, out
    assert "New lesson." in skill.read_text()


@pytest.mark.parametrize("bom_path", ["SKILL.md", ".usage.json", ".bundled_manifest", ".hub/lock.json"])
def test_classification_accepts_bom_prefixed_metadata(policy_home, bom_path):
    from tools.local_skill_policy import classify_root

    root = policy_home / "canonical"
    root.mkdir()
    (root / "SKILL.md").write_text(content("canonical"), encoding="utf-8")
    (policy_home / ".usage.json").write_text(
        json.dumps({"canonical": {"created_by": "agent"}}), encoding="utf-8")
    manifest = policy_home / ".bundled_manifest"
    manifest.write_text("", encoding="utf-8")
    hub = policy_home / ".hub" / "lock.json"
    hub.parent.mkdir()
    hub.write_text(json.dumps({"installed": {}}), encoding="utf-8")
    target = root / bom_path if bom_path == "SKILL.md" else policy_home / bom_path
    target.write_text("\ufeff" + target.read_text(encoding="utf-8"), encoding="utf-8")

    assert classify_root({"path": root}) == ("canonical", "root")
