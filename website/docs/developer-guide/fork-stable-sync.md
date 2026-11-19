---
title: Stable upstream sync for forks
---

# Stable upstream sync for forks

`.github/workflows/sync-upstream.yml` adds **Sync upstream**, an API-only source
synchronization workflow. Adding this file locally does not enable Actions or
synchronize anything. Publication, secret setup, and activation require separate
approval.

## Behavior

- Runs daily at `00:00 UTC` (`07:00` Vietnam time), and on manual
  `workflow_dispatch`. GitHub may delay scheduled runs.
- Derives upstream from the fork repository's `.parent.full_name`, not a
  hardcoded account or a local Git remote. The repository must be a GitHub fork.
- Reads upstream's `/releases/latest` and requires `draft: false`,
  `prerelease: false`, and a strict `vX.Y.Z` tag (numeric components without
  leading zeroes). Canary tags, release candidates, build suffixes, malformed
  metadata, and non-release branch heads are not accepted. An invalid latest
  release fails; the workflow does not scan backward for another release.
- URI-encodes that tag, resolves `/commits/{tag}` to its full commit SHA, and
  submits that SHA as `head` to the fork's `/merges` endpoint. It never uses
  `target_commitish` or upstream `main` as the merge head.
- Uses the selected workflow branch (`GITHUB_REF_NAME`) as the merge `base`.
  Scheduled runs use the repository's default branch (currently `main`); manual
  runs honor the selected branch. Selecting a tag is rejected.
- Preserves fork commits through a normal merge. It does not reset, force-push,
  replace trees, preserve/copy workflow directories specially, or resolve
  conflicts automatically. Upstream workflow changes are ordinary merge inputs.
- Reports HTTP `201` as merged and `204` as already up to date. No release
  (`404`), merge conflicts (`409`), authorization errors, branch protection
  rejections, and other API failures fail visibly without a fallback mutation.

There is no checkout, build, release creation, or dispatch of another workflow.
The workflow updates GitHub source history, not the installed Hermes agent.
**A PAT-authenticated merge can still trigger other enabled workflows that
listen for the resulting repository events**, such as `push`. Before activation,
review inherited CI/publisher workflows and their triggers. If sync-only operation
requires disabling them, obtain separate approval and disable only the selected
workflows; this workflow does not change their state or files.

## Activation and token requirements

Do these only after review and approval:

1. Publish the reviewed workflow to the fork's default branch. GitHub requires
   the workflow on that branch for scheduled runs and discovery of the manual
   trigger; a manual target branch must also contain the reviewed workflow.
2. Leave repository Actions enabled. Forked repositories may require explicitly
   enabling workflows in the **Actions** tab. Enable **Sync upstream** if it is
   disabled. Review other enabled workflows before any run, as noted above.
3. Create a token owned by an account with write access to the fork. Prefer a
   fine-grained personal access token with the fork's owner as **Resource owner**,
   only this fork selected, and these repository permissions:

   | Permission | Access | Purpose |
   | --- | --- | --- |
   | Contents | Read and write | Read repository data and merge the release commit |
   | Workflows | Read and write | Permit merges that add or update `.github/workflows` files |
   | Metadata | Read | GitHub's implicit repository metadata permission |

   The public upstream's release and commit metadata must be readable. A private
   parent needs appropriate additional access. Organization approval/SSO policies
   and token expiration also apply. A classic PAT alternative needs
   `public_repo` (or `repo` for private repository access) plus `workflow` scope.
   Neither token grants rights beyond its owner's access or bypasses branch
   protections/rulesets.
4. In the fork's **Settings → Secrets and variables → Actions → New repository
   secret**, store the token as **`UPSTREAM_SYNC_TOKEN`**. Never paste it into a
   workflow file, documentation, command output, or chat.
5. For an approved first run, open **Actions → Sync upstream → Run workflow** and
   choose the intended branch. Expect either a merge message containing the
   release tag/SHA and destination or an “Already up to date” message. Confirm
   the branch history afterward. A conflict needs manual review/resolution;
   rerunning does not discard fork commits.

The workflow sets `GH_TOKEN` from that secret and gives the unused built-in
`GITHUB_TOKEN` no permissions (`permissions: {}`). **Actions: write is not
required**: there is no dispatch or Actions-management API call. The separate
**Workflows: write** permission is necessary because upstream release merges can
change workflow files; replacing the secret with `GITHUB_TOKEN` is not equivalent.
To pause syncing, disable **Sync upstream** in Actions; no files need deleting.

## Offline regression fixtures

`tests/scripts/test_stable_upstream_sync.py` parses the workflow and executes its
actual Bash block with a strict, local mock `gh` and real `jq`. It covers exact
release-tag SHA merges, non-main manual branches, invalid/pre-release rejection,
repeat `204` no-ops, and conflict/authentication/API failures. It cannot verify
live token permissions, branch rules, GitHub merge behavior, or hosted scheduling.

With an existing project test interpreter (pytest and the checkout's YAML
dependencies), plus Bash and jq, run through the canonical runner:

```bash
scripts/run_tests.sh -j 2 tests/scripts/test_stable_upstream_sync.py
```

This does not need a token and makes no GitHub requests.

References: [Merge a branch API](https://docs.github.com/en/rest/branches/branches#merge-a-branch),
[latest release API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release),
[workflow triggering and PAT behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
