#!/usr/bin/env bash
# THE one feature-set invariant of DebridPulse's aria2, checked against the
# INSTALLED binary (aria2 reports its compiled features itself), so the build
# command is never merely trusted:
#   - BitTorrent is NOT an enabled feature, and no BitTorrent option exists;
#   - HTTPS, SFTP and Metalink are enabled (FTP and plain HTTP are always
#     compiled into aria2 and carry no feature flag).
# Usage: verify-features.sh <expected package version>
set -euo pipefail

expected="$1"
for package in aria2 libaria2-0; do
  installed="$(dpkg-query -W -f='${Version}' "$package")"
  if [ "$installed" != "$expected" ]; then
    echo "$package is $installed, expected $expected" >&2
    exit 1
  fi
done

features="$(aria2c --version | sed -n 's/^Enabled Features: //p')"
echo "aria2c enabled features: $features"
case ", $features," in
  *", BitTorrent,"*) echo "aria2c was built with BitTorrent" >&2; exit 1 ;;
esac
for feature in HTTPS SFTP Metalink; do
  case ", $features," in
    *", $feature,"*) ;;
    *) echo "aria2c lacks $feature" >&2; exit 1 ;;
  esac
done
if aria2c --help=#bittorrent | grep -qE '^ --(bt-|seed-|enable-dht|follow-torrent)'; then
  echo "aria2c still declares BitTorrent options" >&2
  exit 1
fi
echo "aria2 $expected feature set verified"
