# Supply-Chain / Release Artifact Policy

DP 1.0.12 leveling remediation, DEP-001. This document is the explicit
artifact-authority and build-input-hardening policy the audit found missing.
It makes the release contract precise, and it deliberately does **not**
claim bit-identical rebuilds.

## 1. Authoritative release artifact

> One publishable candidate artifact exists per full Git source SHA. Its
> `sha-<40-char-git-sha>` tag is write-once: it is created on first
> publication and is never overwritten. After publication the exact immutable
> OCI manifest digest is the authoritative runtime and attestation identity.
> A candidate is not a release until that digest has passed the required
> qualification workflows.

The rule, exactly as implemented:

- **The source commit identifies one candidate.** The full 40-character Git
  SHA on `1.0.12` is the only authoritative description of "what changed."
  A branch name, tag, or file diff is a pointer to a SHA, never a substitute
  for it. No short-SHA identity is used anywhere in the publication,
  qualification, or promotion chain.
- **The source-SHA tag is write-once.** `Fork Image`'s `publish` job derives
  exactly one tag, `ghcr.io/xipher-zero/debridpulse:sha-<full-git-sha>`.
  Before building anything it checks whether that tag already exists:
  - **absent** — the job builds and pushes the multi-architecture image once;
  - **present, recorded `org.opencontainers.image.revision` equals the exact
    `GITHUB_SHA`, both linux platform children present** — the job reuses the
    existing candidate: it builds nothing, pushes nothing, and carries the
    existing digest into verification;
  - **present but the recorded revision is anything else, or the registry
    check cannot be answered conclusively** — the job fails closed. It never
    overwrites the tag and never guesses.

  The check happens *before* publication; the job does not push first and
  detect replacement afterwards.
- **Runs for one source SHA are serialized.** `Fork Image` declares a
  workflow-level concurrency group keyed on the full `github.sha` with
  `cancel-in-progress: false`, so two runs for the same commit (for example a
  branch push and a tag push at one commit) cannot race through the
  existence check and publication. The second run reaches the existence check
  only after the first has finished, finds a valid candidate, and reuses it.
  GHCR offers no conditional tag write, so this serialization plus the
  pre-publish check is the enforcement mechanism for write-once.
- **A rerun is idempotent.** Re-running `Fork Image` for a SHA that already
  has a candidate reuses that candidate and reports its existing digest.
- **A new candidate requires a new source SHA.** A change that should
  produce a different image — a base-image refresh (§3), a package-set change
  (§4), a dependency refresh (§5), any Dockerfile change — is a new source
  commit, which is a new candidate under its own `sha-<full-git-sha>` tag.
  "Same Git SHA, different image digest" is never a second candidate under
  one source identity.
- **Manifest-list digest authority.** The `sha256:...` manifest-list (index)
  digest behind the `sha-<full-git-sha>` tag is the authoritative identity
  of the candidate once published — not the tag string, which exists only to
  locate it.
- **Per-architecture child qualification.** `Container Security` and
  `Candidate Runtime Qualification` resolve
  `${IMAGE_NAME}:sha-${GITHUB_SHA}`, verify the index records the full
  source revision, resolve the `amd64` and `arm64` child digests, and qualify
  each child individually by digest (vulnerability scan, non-root runtime,
  health, integration behavior, archive round-trip). A candidate is not
  qualified until both children pass.
- **SBOM / provenance / attestation.** `Fork Image` publishes with
  `provenance: mode=max` and an SBOM; `Container Security` and `Candidate
  Runtime Qualification` each write and sign an in-registry attestation
  (`container-security/v1`, `candidate-runtime/v1`) bound to the exact
  digest they qualified. `release-promotion.yml` re-verifies both
  attestations target that same digest before promoting.
- **Mutable release tags only point to previously qualified digests.**
  `release-promotion.yml` requires a full 40-character `candidate_sha`,
  derives `source_tag=sha-<candidate_sha>`, resolves the immutable source
  digest, verifies the recorded revision, verifies both attestations against
  that digest, and only then runs `docker buildx imagetools create` to move
  `latest` (or a `v*`/`internal-v*` tag) onto it. Promotion never triggers a
  build. A fix needed at promotion time is a new source commit and therefore
  a new candidate that must independently clear every required gate first.

## 2. Rebuilds are not assumed reproducible

Building the exact same Dockerfile against the exact same Git SHA at a later
time is **not** assumed to produce an identical digest, because:

- Debian package repositories (`apt-get install`) serve whatever package
  versions are current on the mirror at build time, not a snapshot pinned to
  the Dockerfile's authoring date.
- The base image digest, once refreshed (see §3), changes the base
  filesystem candidate for every subsequent build until refreshed again.
- Even with a hash-pinned Python lock (§5) and a digest-pinned base image
  (§3), OS-level package drift alone is enough to change layer digests
  between two builds of the identical source tree on different days.

This is why the write-once rule in §1 exists: the workflow never rebuilds a
SHA that already has a candidate, so the drift described here cannot move a
published `sha-<full-git-sha>` tag to a different artifact. This policy does
not attempt to eliminate the drift itself with vendored/mirrored Debian
package snapshots — that would be a materially larger, separate change.
Instead, it accepts the drift explicitly and compensates with the per-digest
qualification model in §1: **every published candidate is qualified as
itself**, never assumed equivalent to a build of the same source made at a
different time.

## 3. Base image digest

The Dockerfile pins the Python base image by verified multi-architecture
manifest-list digest:

```dockerfile
FROM python:3.12.14-slim-trixie@sha256:<verified manifest-list digest>
```

The pinned digest must resolve to a **manifest list / OCI index** (not a
single-architecture child manifest) containing at least `linux/amd64` and
`linux/arm64`, verified with a trusted registry inspection command, e.g.:

```bash
docker buildx imagetools inspect python:3.12.14-slim-trixie
```

or an equivalent registry API query. Never hand-type or guess a digest.

**Base-image digest refresh procedure:**

1. Re-resolve the current manifest-list digest for the intended tag
   (`python:3.12.14-slim-trixie`, or a newer patch/minor tag when
   deliberately upgrading Python).
2. Update the `FROM` line's digest.
3. Push to `1.0.12`. This produces a new source SHA and therefore a new
   candidate, which must clear every required qualification gate (§1) before
   any promotion.
4. Never edit only the digest without also expecting and running full
   requalification — a base refresh is exactly the kind of change §1's
   "a new candidate requires a new source SHA" rule exists for.

## 4. Debian package refresh policy

`apt-get install --no-install-recommends` installs whatever package versions
the pinned base image's configured repositories currently serve. This
Dockerfile does **not** run a blanket `apt-get upgrade` — the base image
digest (§3) already fixes the base filesystem candidate, and an unbounded
upgrade would reintroduce exactly the kind of undeclared drift this policy
exists to make explicit.

Security freshness for the apt-installed layer (`aria2`, `curl`, `gosu`,
`zstd`, `7zip`, `7zip-rar`) is enforced by:

- deliberate, periodic base-image digest refresh (§3), which picks up
  whatever patched packages the new base snapshot carries;
- the existing `Container Security` Trivy scan (fixable HIGH/CRITICAL fails
  the gate) on every qualification cycle, plus its weekly scheduled rerun
  against the currently-promoted digest;
- requalification of the resulting new digest before any promotion.

There is no separate "pin exact apt package versions" mechanism in this
policy; that would require vendoring or mirroring Debian's package pool,
which is out of scope for this remediation. If a specific package needs an
explicit floor (a CVE fix not yet in the pinned base's repository snapshot),
address it as its own scoped change, not as an DEP-001-wide requirement.

**Observed in practice:** the first exact-SHA `Container Security` run after
removing the blanket `apt-get upgrade` (this remediation) found the pinned
base digest already carried fixable HIGH/CRITICAL CVEs in base-layer packages
this Dockerfile never explicitly installs (`gzip`, `libpcre2-8-0`,
`libsqlite3-0`, `perl-base`) — re-querying the registry confirmed no newer
manifest-list digest existed yet for the pinned tag. The gate is working as
designed (§2: "every published candidate is qualified as itself"); the correct
response is exactly the scoped floor described above: a deliberate, NAMED
`apt-get install --only-upgrade <exact packages>` line for only the packages
Trivy flagged, not a reversion to blanket upgrade and not a version pin
(Debian's repository remains a moving target regardless — §2). Drop that
line once a base-digest refresh (§3) already carries the fix, rather than
accumulating named-package upgrades indefinitely.

## 5. Python dependency lock and hash refresh procedure

`backend/requirements.txt` is generated from `backend/requirements.in` with
hashes for every resolved package (direct and transitive) via `pip-tools`:

```bash
cd backend
python -m piptools compile \
  --generate-hashes \
  --strip-extras \
  --output-file=requirements.txt \
  requirements.in
```

Run this with `requirements.txt` already present at the output path so
`pip-compile` reuses currently-pinned versions and only changes a version
when `requirements.in` (or a transitive constraint) forces it — an
opportunistic version bump is not an expected side effect of a hash refresh.

The Docker build installs with hash enforcement:

```dockerfile
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
```

`--require-hashes` makes `pip` refuse to install anything — including a
transitive dependency — whose downloaded artifact does not match one of the
recorded hashes. Every package in `requirements.txt` must carry at least one
`--hash=sha256:...` entry, or the install fails closed rather than silently
accepting an unverified artifact.

**Refresh procedure:** regenerate `requirements.txt` (above) whenever
`requirements.in` changes, or periodically to pick up transitive security
fixes; either way this is a normal source change that produces a new
source SHA (a new candidate) and must clear qualification (§1) before
promotion, exactly like any other change.

`requirements-dev.txt` and `requirements-qa.txt` are not hash-pinned by this
policy — they are development/CI-only inputs, never installed into the
shipped runtime image. If they are hash-pinned in the future, do so
consistently and update every CI install command in the same change; a
half-hashed include graph (some files hash-pinned, others not, feeding into
each other) is worse than none.

## 6. Any build-input refresh requires requalification

To make the invariant explicit and non-optional: refreshing **any** of the
following is a new source commit and therefore a new candidate, full stop —

- the pinned base image digest (§3);
- the Debian package set actually installed (indirectly, via a base refresh
  or an `apt-get install` package-list change);
- `backend/requirements.in` / the regenerated hash-pinned
  `backend/requirements.txt` (§5);
- the Dockerfile's own build steps.

None of these may be promoted to a mutable tag (`latest`, a `v*` release
tag) without independently clearing `Tests`, `CodeQL`, `Browser Runtime`,
`Container Security`, and `Candidate Runtime Qualification` on the exact
resulting digest, per the existing `release-promotion.yml` gate.
