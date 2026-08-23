# v1.0.6 Security Audit Corrections

Date: 2026-08-23

This note records the corrective work from the post-release adversarial security review of DebridPulse v1.0.6. It is intentionally narrow and does not redefine the general security policy in `SECURITY.md`.

## Corrected trust boundaries

### Provider-issued download URLs

User-submitted direct links were already restricted to HTTP(S), but the capability URL returned by AllDebrid crossed a second network trust boundary before being sent to local aria2. v1.0.6 now validates provider-issued URLs as well. Provider URLs must use HTTP(S), must not contain URL credentials, and literal/local destinations (loopback, link-local, private, unspecified, multicast, localhost/mDNS names, and legacy numeric loopback forms) are rejected before aria2 receives them. The same validation is applied to links returned by the AllDebrid magnet-file manifest.

This is defense in depth around a provider response. It does not replace normal network segmentation or host firewall policy.

### Native archive parser selection

Automatic extraction uses Python-native readers for ZIP/TAR/GZIP/BZIP2/XZ and Debian 7-Zip for 7z, RAR, and the composite formats that require it.

The security review identified that invoking 7-Zip without an explicit type leaves parser selection to filename/content detection. v1.0.6 now:

- pins `.7z` input to the 7z parser;
- pins RAR/multipart-RAR input to the RAR parser;
- preserves the recursive parser mode required for `.tar.zst` / `.tar.lzma`, but explicitly excludes the XZ parser there;
- uses the same parser policy for preflight listing and extraction.

This closes the identified path by which a mismatched accepted filename could be handed to 7-Zip's XZ decoder while preserving the supported composite archive behavior.

## Runtime dependency coverage

The existing CI gates Python dependencies with `pip-audit` and source with Bandit/CodeQL. A new `Container Security` workflow now builds the actual release image and scans OS and language packages with Trivy.

Policy:

- Medium/High/Critical runtime findings are reported in CI, including unfixed advisories.
- High/Critical findings with an available fix fail the workflow.
- Unfixed findings remain visible for explicit review rather than making every build permanently red when no remediation exists.
- The workflow runs on main/staging pushes and pull requests, manually, and weekly so newly published CVEs can surface without a source-code change.

### Known temporary dependency risk: aria2 CVE-2026-8367

At the time of this correction Debian Trixie's aria2 1.37.0 package is affected by CVE-2026-8367, a TLS certificate Extended Key Usage validation flaw. The issue is currently treated as a temporary upstream/runtime dependency risk because a fixed Debian package is not yet available. It remains visible through the container vulnerability report and should be removed from the accepted-risk set as soon as a fixed package is published.

## Non-root runtime validation

The production entrypoint already drops from startup root privileges to the configured `PUID`/`PGID`, but the existing image smoke test deliberately used root. `Container Security` now launches the built image with a synthetic non-root UID/GID, verifies PID 1 actually runs as that identity, exercises the health endpoint, and verifies the runtime identity can write only the expected application/download directories needed for normal operation.

## Remaining release checklist item

A successful CodeQL workflow means analysis completed and results were uploaded; it is not by itself proof that the repository has zero open code-scanning alerts. Before final promotion, inspect the repository's Code Scanning Alerts view and record whether any open alert is applicable to the release head.
