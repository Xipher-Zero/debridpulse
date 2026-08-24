# DebridPulse Release and Repository Operations

This document is the authoritative operational runbook for DebridPulse source promotion and release work.

## Source-of-truth boundary

**There is no assistant-controlled local Git development workspace for DebridPulse.**

Source changes, branch management, pull requests, merge operations, and release preparation are performed against the GitHub repository through the connected GitHub tooling.

The deployment/validation host may have a Compose project directory (historically `/home/xipher/alldebrid-client`), but that directory is a **runtime/deployment surface**, not the development source-of-truth. Do not ask the operator to run `git tag`, `git push`, branch-management commands, or source-development commands there merely because a shell is available.

Use shell commands on the deployment host only for things that actually require the running installation: pulling a candidate image, changing the Compose image reference, recreating the service, querying health, controlled failure injection, filesystem inspection, and other local acceptance tests.

## Normal development flow

1. Perform source edits through the GitHub connector on the active staging/feature branch.
2. Re-read changed files/diffs after writes. Verify that only intended files changed.
3. Run the permanent CI matrix (Tests, CodeQL, Container Security, Fork Image) on the candidate SHA.
4. Prefer immutable candidate images (`sha-<shortsha>`) for local runtime validation.
5. Before replacing the deployed image, verify the OCI label `org.opencontainers.image.revision` exactly matches the expected full candidate SHA.
6. Perform local behavioral acceptance against that exact image.
7. Freeze the accepted SHA. Do not make opportunistic changes after acceptance; any source change creates a new candidate and requires requalification.
8. Merge the PR using expected-head protection so GitHub rejects the merge if the accepted PR head moved.
9. Verify `main` points to the expected merge commit and that the accepted candidate is the intended merge parent.
10. Publish the release and retire the staging branch only after the merge and release preconditions are verified.

## Release identity

Before publication, verify all release surfaces agree:

- `VERSION`
- top `CHANGELOG.md` entry
- README/install examples
- Compose example image tag
- project landing page, if present
- PR title/body and release notes
- OCI image `version` and `revision` labels

Do not reuse an existing historical Git tag. If inherited/upstream tags occupy the next apparent version numbers, advance to the next available release identity rather than deleting or rewriting historical tags.

## Preferred GitHub operations

Use direct connector primitives whenever they exist:

- file/source writes: GitHub contents operations
- PR metadata: GitHub PR operations
- merge: merge PR with `expected_head_sha`
- CI inspection: commit workflow runs / job logs
- branch/ref inspection: repository/branch/ref operations

Do not conclude that an operation is impossible merely because a narrowly filtered tool discovery did not expose it. If necessary, discover the full GitHub tool surface first.

## Remote Actions fallback for missing repository primitives

When the connector does not expose a required GitHub-side primitive (for example tag creation, GitHub Release creation, workflow dispatch in a needed form, or branch-ref deletion), **do not hand the operation back to the user and do not invent a local repo**.

Use the established one-shot remote Actions pattern.

### Pattern

1. Create a temporary branch such as `release-ops/vX.Y.Z` from the **exact already-merged release commit**.
2. Modify an already-registered workflow **only on that temporary branch** so a push to the temporary branch starts the one-shot job. This avoids modifying `main`.
3. Grant the one-shot job only the permissions it needs, normally:
   - `contents: write`
   - `packages: write` when publishing GHCR
4. Make the job fail closed before any destructive operation. Verify at minimum:
   - `main` still equals the expected merge SHA;
   - `VERSION` equals the intended release version;
   - the staging branch still equals the exact locally accepted candidate SHA;
   - any pre-existing release tag either does not exist or already points at the expected release commit.
5. Checkout the **exact release commit SHA**, not the temporary workflow commit, for build/publication work.
6. Perform the missing GitHub operations with the runner's authenticated `gh api` / GitHub token.
7. Verify each created ref/resource before proceeding to deletion.
8. Delete the staging branch only if it still points at the accepted SHA.
9. Delete the temporary release-operations branch as the final success-gated action.
10. Independently verify afterward that:
    - the tag resolves;
    - the GitHub Release exists;
    - the release image exists with the expected OCI revision;
    - the staging branch is absent;
    - the temporary operations branch is absent;
    - `main` was not altered by the helper.

### Important GitHub Actions behavior

A tag or ref created with the repository `GITHUB_TOKEN` does **not** reliably trigger another workflow from that generated event. Therefore, a one-shot release runner must not assume that creating `vX.Y.Z` will cause the normal tag-publish workflow to run.

If the release image must be published in the same operation, explicitly build/publish the versioned image in the one-shot runner from the exact release commit, or use another authenticated mechanism whose events are intentionally allowed to trigger the normal workflow.

Never create a branch named like a tag as a substitute for a real tag.

## Release-image contract

For a public release image:

- build from the exact merged release commit;
- publish the versioned tag, e.g. `ghcr.io/xipher-zero/debridpulse:vX.Y.Z`;
- include multi-arch targets expected by the permanent release workflow;
- preserve SBOM/provenance generation;
- set OCI labels including:
  - `org.opencontainers.image.version=X.Y.Z`
  - `org.opencontainers.image.revision=<full release commit SHA>`
  - `org.opencontainers.image.source=https://github.com/Xipher-Zero/debridpulse`
  - `org.opencontainers.image.licenses=GPL-2.0-or-later`

The OCI revision is the authoritative check when moving a deployment from a candidate SHA image to the published release tag.

## Local deployment / acceptance rule

The deployment host is for **runtime testing**, not repository administration.

For candidate or release deployment:

1. Pull the requested image before changing Compose.
2. Inspect `org.opencontainers.image.revision` and require exact equality with the expected SHA.
3. Back up the Compose file.
4. Change only the image reference.
5. Run `docker compose config --quiet`.
6. Recreate only the DebridPulse service.
7. Inspect container image/status/health.
8. Query the internal `/api/health` endpoint.
9. Restore the previous Compose file/image if validation or recreation fails.

For staging candidates, use immutable `sha-<shortsha>` images. After release publication, move production to the version tag only after verifying the version tag's OCI revision equals the promoted `main` release commit.

## Promotion gate

A release is ready for promotion only when all applicable gates are green:

- source/architecture audit
- functionality/result-authority audit
- security audit
- V1 scope audit
- license/provenance audit
- release-surface/version audit
- permanent CI/security/image matrix
- exact-head local behavioral acceptance

Behavioral acceptance should exercise the subsystems materially changed by the release. A green unit suite is not a substitute for the relevant real runtime path.

## Closeout checklist

After release publication:

- PR merged and closed;
- `main` verified at the intended merge commit;
- release tag verified at the intended commit;
- GitHub Release published;
- GHCR version image published and OCI revision verified;
- staging branch deleted;
- temporary release-operations branch/workflow changes absent;
- final local deployment moved from candidate SHA tag to the public version tag when appropriate;
- acceptance evidence preserved in the merged PR/release record.

If a future session cannot remember how a repository-side operation was performed, **read this document before asking the operator to perform GitHub administration manually**.
