"""Deployment-local skill policy; loaded explicitly, never patches Hermes modules.

Source protection and curator ownership are independent. Hermes' bundled manifest,
hub lock and external-path resolver establish installed source; usage establishes
management/pins, not authorship. Missing/invalid ownership remains unresolved.
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ROUTING_POLICY = (
    "Protected skills and local extension routing (ENFORCED):\n"
    "Source provenance and curator ownership are separate. Resolve the actual root SKILL.md "
    "and its canonical frontmatter name, not a category/input alias. Check installed source "
    "records (bundled manifest, hub lock, external path) separately from the scoped .usage.json "
    "record keyed by that canonical name. A source label, author, prior load, or created_by "
    "alone is not proof of source provenance. skills_list/skill_view alone do not establish "
    "curator ownership or pin state.\n"
    "Bundled/builtin, official/hub-installed and external-origin roots are protected, even "
    "when adopted. Local roots that are pinned, not curator-managed, or not writable are "
    "also off-limits to autonomous edits. For a positively protected root, create or update "
    "`<canonical-skill-name>-extension` without editing the original. An existing extension "
    "must independently pass ownership, pin, provenance, approval and read-before-write guards.\n"
    "For a verified local, curator-managed, unpinned and writable root: Patch the root "
    "directly; do not create or update an unnecessary extension. Missing or ambiguous root "
    "provenance/ownership: do nothing to either artifact, report what could not be verified. "
    "Never adopt or unpin automatically. Nothing to save remains valid when no durable "
    "learning exists; protection alone is not a reason to discard a useful extension lesson.\n\n"
)


def transform_review_prompt(prompt):
    """Transform only routing clauses; leave learning/memory/read rules intact.

    Stock prompt drift fails visibly instead of silently retaining contradictory rules.
    Per-agent custom prompts without the stock block retain their body plus this policy.
    """
    heading = "Protected skills (DO NOT edit these):\n"
    if heading not in prompt:
        alternatives = (
            ("whoever wrote it: hand-written, installed, bundled, and pinned skills are all yours to improve.",
             "subject to the verified root/extension routing below."),
            ("the right place, whoever wrote it.",
             "the right place, subject to the verified root/extension routing below."),
        )
        hits = sum(prompt.count(old) for old, _ in alternatives)
        stock = "UPDATE A CURRENTLY-LOADED SKILL." in prompt
        if hits:
            if hits != 1 or "Read-before-write (ENFORCED" not in prompt:
                raise ValueError("Upstream review routing changed; inspect local policy transform before reviewing")
            for old, replacement in alternatives:
                prompt = prompt.replace(old, replacement)
            position = prompt.index("Read-before-write (ENFORCED")
            return prompt[:position] + ROUTING_POLICY + prompt[position:]
        if stock:
            raise ValueError("Upstream review routing changed; inspect local policy transform before reviewing")
        return prompt + "\n\n" + ROUTING_POLICY
    start = prompt.index(heading)
    end = prompt.index("Do NOT capture", start)
    prompt = prompt[:start] + ROUTING_POLICY + prompt[end:]
    alternatives = (
        "to you no matter how relevant (see Protected skills below); for those, fall through to the next option.",
        "off-limits however relevant; fall through when one of those is the best fit.",
    )
    hits = sum(prompt.count(old) for old in alternatives)
    if hits != 1:
        raise ValueError("Upstream review routing changed; inspect local policy transform before reviewing")
    for old in alternatives:
        replacement = ("to direct edits; use the verified root/extension routing below."
                       if old.startswith("to you") else
                       "off-limits to direct edits; use the verified root/extension routing below.")
        prompt = prompt.replace(old, replacement)
    return prompt


def canonical_name(record):
    from agent.skill_utils import parse_frontmatter
    root = Path(record["path"])
    metadata, _ = parse_frontmatter((root / "SKILL.md").read_text(encoding="utf-8-sig"))
    name = metadata.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Resolved root has no canonical frontmatter name")
    return name


def classify_root(record):
    """Return (canonical name, route); protection never follows from created_by."""
    from agent.skill_utils import is_external_skill_path
    from tools import skill_usage
    root = Path(record["path"])
    name = canonical_name(record)
    if is_external_skill_path(root):
        return name, "extension"
    # Upstream source readers deliberately swallow corrupt metadata. Validate it
    # first here: corruption must not turn an imported root into a local one.
    base = skill_usage._skills_dir()
    manifest = base / ".bundled_manifest"
    if manifest.exists():
        manifest.read_text(encoding="utf-8-sig")
    hub_lock = base / ".hub" / "lock.json"
    if hub_lock.exists():
        hub = json.loads(hub_lock.read_text(encoding="utf-8-sig"))
        if not isinstance(hub, dict) or not isinstance(hub.get("installed", {}), dict):
            raise ValueError("Invalid hub provenance")
    if any(predicate(name) for predicate in (
        skill_usage.is_protected_builtin, skill_usage.is_hub_installed, skill_usage.is_bundled
    )):
        return name, "extension"
    usage = json.loads((base / ".usage.json").read_text(encoding="utf-8-sig"))
    rec = usage.get(name) if isinstance(usage, dict) else None
    if not isinstance(rec, dict) or not ({"created_by", "agent_created"} & rec.keys()):
        raise ValueError("Root has no ownership record")
    if not isinstance(rec.get("pinned", False), bool):
        raise ValueError("Invalid pin state")
    if rec.get("pinned") or not skill_usage._is_curator_managed_record(rec):
        return name, "extension"
    if not (os.access(root, os.W_OK) and os.access(root / "SKILL.md", os.W_OK)):
        return name, "extension"
    return name, "root"


def extension_root_guard(name, action="create"):
    from tools.skill_provenance import is_background_review
    from tools import skill_manager_tool
    if not is_background_review() or not isinstance(name, str) or not name.endswith("-extension"):
        return None
    root_alias = name[:-len("-extension")]
    prefix = f"Refusing background curator {action} for '{name}': "
    try:
        record = skill_manager_tool._find_skill(root_alias)
        if not record:
            return {"success": False, "error": prefix + f"root '{root_alias}' does not exist. Do not create an orphan extension."}
        canonical, route = classify_root(record)
        if route == "extension":
            return None  # permission for the ROOT route, not for the target write
        return {"success": False, "error": prefix + f"root '{canonical}' is local, curator-managed, unpinned, and writable. Patch the root skill instead."}
    except Exception:
        logger.warning("Extension root classification failed for %s", name, exc_info=True)
        return {"success": False, "error": prefix + "root classification could not be verified safely. Verify source provenance and ownership before writing either root or extension."}
