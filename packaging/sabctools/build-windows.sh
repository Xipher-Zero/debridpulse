#!/usr/bin/env bash
# sabctools for DownloadPulse's one Python on Windows x64 (CPython 3.12, the
# python.org build): the exact sdist DownloadPulse's lock pins
# (backend/requirements.txt), built by its own setup.py with Debian's UCRT
# mingw-w64 toolchain (python.org CPython's C runtime) against the headers and
# python312.dll of the python.org NuGet package, with one change:
# windows-mingw.patch (no libdl, header spelling, explicit casts). PyPI has no
# cp312 Windows wheel for this version.
#
# Usage: build-windows.sh <work-dir> <out-dir>
# Produces <out-dir>/sabctools-<version>-cp312-cp312-win_amd64.whl, the
# extension's imported DLLs (imports.txt), <out-dir>/source/ (sdist + patch +
# this script) and the license.
set -euo pipefail

HOST=x86_64-w64-mingw32ucrt
SABCTOOLS=9.6.3
SDIST_SHA256=faacd20d5ec9111e39c81326ca1e49a2be2b24b95c13c28f82e3e7ac198c9129
PYTHON=3.12.10
NUGET_SHA512=u9pNz2iKlCEbYtUJaKkbOPMF0LjR7NkCafdKhvigpPzrt8oWKgdTpHaR6z3wyWQAm9PYGUxv0Zr66NX9AeHMDw==
here="$(cd "$(dirname "$0")" && pwd)"
work="$1"
out="$2"
mkdir -p "$work" "$out/source"
cd "$work"

# The sdist DownloadPulse's lock pins, and the python.org NuGet package.
curl -sSfL -o "sabctools-$SABCTOOLS.tar.gz" \
  "https://files.pythonhosted.org/packages/source/s/sabctools/sabctools-$SABCTOOLS.tar.gz"
echo "$SDIST_SHA256  sabctools-$SABCTOOLS.tar.gz" | sha256sum -c --quiet
curl -sSfL -o python.nupkg "https://api.nuget.org/v3-flatcontainer/python/$PYTHON/python.$PYTHON.nupkg"
test "$(openssl dgst -sha512 -binary python.nupkg | base64 -w0)" = "$NUGET_SHA512"
python3 -c "import zipfile; zipfile.ZipFile('python.nupkg').extractall('python')"
include="$work/python/tools/include"
grep -qE "PY_VERSION[[:space:]]+\"$PYTHON\"" "$include/patchlevel.h"

tar -xzf "sabctools-$SABCTOOLS.tar.gz"
cd "sabctools-$SABCTOOLS"
patch -p1 --forward --fuzz=0 < "$here/windows-mingw.patch"
CC="$HOST-gcc" CXX="$HOST-g++" LDSHARED="$HOST-g++ -shared -static-libgcc -static-libstdc++ -Wl,-Bstatic,--whole-archive -lwinpthread -Wl,--no-whole-archive,-Bdynamic" \
  LDCXXSHARED="$HOST-g++ -shared -static-libgcc -static-libstdc++ -Wl,-Bstatic,--whole-archive -lwinpthread -Wl,--no-whole-archive,-Bdynamic" \
  CFLAGS="-I$include -DMS_WIN64" CXXFLAGS="-I$include -DMS_WIN64" \
  LDFLAGS="-L$work/python/tools -lpython312" SETUPTOOLS_EXT_SUFFIX=.cp312-win_amd64.pyd \
  python3 setup.py -q bdist_wheel --plat-name win_amd64 --dist-dir "$out" >/dev/null
wheel="$(find "$out" -maxdepth 1 -name "sabctools-$SABCTOOLS-cp312-cp312-win_amd64.whl")"
test -n "$wheel"
python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); z.extract('sabctools/sabctools.cp312-win_amd64.pyd', sys.argv[2])" "$wheel" "$work/inspect"
printf 'sabctools.cp312-win_amd64.pyd: ' > "$out/imports.txt"
$HOST-objdump -p "$work/inspect/sabctools/sabctools.cp312-win_amd64.pyd" | sed -n 's/^\s*DLL Name: //p' | tr '\n' ' ' >> "$out/imports.txt"
echo >> "$out/imports.txt"
cat "$out/imports.txt"
# Python's own DLL, Windows' and the UCRT's only (no MinGW runtime DLL).
if grep -viE '^[a-z0-9_.-]+\.pyd: ((python312|KERNEL32|USER32|ADVAPI32|WS2_32|api-ms-win-[a-z0-9-]+)\.dll ?)+$' "$out/imports.txt"; then
  echo "the sabctools extension imports an unexpected DLL" >&2
  exit 1
fi
cp LICENSE.md "$out/copyright"
cp "$work/sabctools-$SABCTOOLS.tar.gz" "$here/windows-mingw.patch" "$here/build-windows.sh" "$out/source/"
echo "built $(basename "$wheel")"
