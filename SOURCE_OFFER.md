# Corresponding source offer

The complete source code for DebridPulse is available from the repository identified
by the container image's `org.opencontainers.image.source` label. The exact
revision used to build an image is recorded in its
`org.opencontainers.image.revision` label.

For GPL- or LGPL-covered third-party binaries distributed in an official DebridPulse
container image, Chris Moore offers to provide the complete corresponding
source code, including the material needed to rebuild that software, to any
third party for no more than the reasonable cost of physically providing the
source. This offer is valid for three years after the last distribution of the
applicable image version.

Request source using the repository's dedicated source-request form:
<https://github.com/Xipher-Zero/debridpulse/issues/new?template=source_request.yml>.
Include:

- the full container image name and digest;
- the target architecture;
- the package name, if known; and
- a preferred machine-readable delivery method.

The image preserves Debian package copyright files under `/usr/share/doc` and
common license texts under `/usr/share/common-licenses`. Its published SBOM
attestation records the installed package versions needed to identify the
corresponding source precisely.

## Where the corresponding source is

The written offer above remains available for any of the following.

- **DebridPulse:** the repository named by the image's
  `org.opencontainers.image.source` label, at the exact commit in its
  `org.opencontainers.image.revision` label.
- **SABnzbd (the bundled Usenet service):** its complete upstream source
  release is unpacked unchanged in the image at `/app/usenet`. It is also
  published upstream at `https://github.com/sabnzbd/sabnzbd/releases/tag/<version>`.
- **Python packages:** each package and version is listed in
  `licenses/python-runtime.json` under its PyPI name and version, where its
  published source can be retrieved. Pure-Python packages are installed as source in the image
  (`/usr/local/lib/python3.12/site-packages`).
- **Debian packages, including rsync:** the exact version of every installed
  Debian package is recorded in the image's SBOM attestation. Its source
  package is available from Debian's archive, either with
  `apt-get source <package>=<version>` on Debian trixie or from
  `https://snapshot.debian.org/package/<source package>/<version>/`.
  - The rsync binary, `/usr/bin/rsync` (GPL-3.0-or-later), is installed from
    Debian's `rsync` package. In the image built from commit
    `a78055bdfb96bbc59b32a42150c5b0d648085c26` its version is
    `3.5.0+ds1-0+deb13u1`.
  - The corresponding source for that version is
    `https://snapshot.debian.org/package/rsync/3.5.0%2Bds1-0%2Bdeb13u1/`
    (`rsync_3.5.0+ds1-0+deb13u1.dsc`, `rsync_3.5.0+ds1.orig.tar.xz` and
    `rsync_3.5.0+ds1-0+deb13u1.debian.tar.xz`), or
    `apt-get source rsync=3.5.0+ds1-0+deb13u1`.
  - An image built later may carry a newer Debian stable update; its SBOM
    names that version.
- **DebridPulse's aria2 and FFmpeg packages:** these are rebuilt from Debian's
  source. Each package artifact the Dockerfile names by digest carries its
  complete corresponding source in `/source/`: Debian's source package plus
  DebridPulse's patches or build script, which are also in this repository
  under `packaging/aria2/` and `packaging/ffmpeg/`
  (`docs/SUPPLY_CHAIN_POLICY.md`, sections 4a and 4b).
