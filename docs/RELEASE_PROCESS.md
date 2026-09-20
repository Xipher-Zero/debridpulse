# DebridPulse Release and Repository Operations

This document is the authoritative operational runbook for DebridPulse source promotion and release work.

## Source-of-truth boundary

Source changes, branch management, promotion and release preparation are performed against the source repository — either through the connected GitHub tooling or in a clone of the repository dedicated to development.

**The deployment/validation host is not that workspace.** It may have a Compose project directory (historically `/home/xipher/alldebrid-client`), but that directory is a **runtime/deployment surface**, not the development source-of-truth. Do not run `git tag`, `git push`, branch-management commands, or source-development commands there merely because a shell is available.

Use shell commands on the deployment host only for things that actually require the running installation: pulling a candidate image, changing the Compose image reference, recreating the service, querying health, controlled failure injection, filesystem inspection, and other local acceptance tests.

## Normal release flow — exact-SHA promotion

Releases are promoted by **exact SHA**, not by merging a pull request. `main` is
fast-forwarded to the exact commit that hosted CI already qualified, so the commit,
the tree, the tag and the published image all name the same artifact.

1. Develop and qualify on the release branch (for example `1.0.12`).
2. Re-read changed files/diffs after writes. Verify that only intended files changed.
3. Freeze and approve an exact source tree/SHA. Any later source change creates a new
   candidate and requires requalification from zero.
4. Push the exact approved release-branch commit.
5. Let the required hosted workflows qualify **that exact SHA**: Tests, Browser Runtime,
   CodeQL, Fork Image, Container Security, Candidate Runtime Qualification (and
   WS3 Adversarial on branches that run it). Do not rerun until green; classify a failure
   with the candidate-vs-anchor policy in `docs/QUALIFICATION_DETERMINISM.md`.
6. Prefer the immutable candidate image (`sha-<full-git-sha>`) for local runtime validation,
   and verify the OCI label `org.opencontainers.image.revision` exactly equals the expected
   full candidate SHA before replacing a deployed image.
7. Perform the final audit against the frozen branch state.
8. When the candidate is accepted as DONE, promote that exact release commit/tree to `main`
   by fast-forward:

   ```bash
   git push origin <FULL_RELEASE_SHA>:refs/heads/main
   ```

   **Promotion must not introduce source changes.** No merge commit, no squash, no rebase,
   no edit — anything that changes the tree produces a SHA that nothing has qualified, and
   the promotion gate will refuse it. `main`'s resulting tree must equal the accepted
   release tree exactly.
9. `latest` is promoted from the already-qualified immutable `sha-<full-git-sha>` digest by
   the Release Promotion workflow. It is **never rebuilt** as a separate release artifact.
10. Create the version tag so it resolves to the exact final release commit, and verify the
    version image tag resolves to the same qualified immutable manifest digest.
11. Create the GitHub Release from the canonical bracketed changelog entry for that version.
12. The release is closed only after every identity check agrees (see *Release identity*).

### Why not a PR merge

`release-promotion.yml` resolves the candidate as `$GITHUB_SHA` on a push to `main` and
requires every required workflow to be **completed and successful for that exact SHA**,
polling and then failing closed. A merge commit is a new SHA that nothing has qualified,
so it would either fail the gate or force a requalification that promotes a *different*
image digest than the one that passed every gate. Expected-head PR merging is therefore no
longer the canonical release path.

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

- file/source writes: GitHub contents operations, or ordinary commits in a local clone of the source repository
- promotion: fast-forward push of the exact qualified release SHA to `main`
- tag/release: annotated tag at the exact release commit, then a GitHub Release built from the canonical changelog entry
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

For staging candidates, use immutable `sha-<full-git-sha>` images. After release publication, move production to the version tag only after verifying the version tag's OCI revision equals the promoted `main` release commit.

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

- `main` verified at the exact qualified release SHA, with a tree identical to the accepted release tree;
- release tag verified at that same commit;
- GitHub Release published from the canonical changelog entry;
- GHCR version image published and OCI revision verified;
- `latest` and the version tag resolve to the same immutable qualified manifest digest;
- temporary release-operations branch/workflow changes absent;
- final local deployment moved from candidate SHA tag to the public version tag when appropriate;
- acceptance evidence preserved in the release record.

If a future session cannot remember how a repository-side operation was performed, **read this document before asking the operator to perform GitHub administration manually**.
