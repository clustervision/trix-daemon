#!/bin/bash

# This code is part of the TrinityX software suite
# Copyright (C) 2026  ClusterVision Solutions b.v.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>

# Inside one EL container: luna2-python from ClusterVision's repository, trixd's
# requirements into it, then the unit tests and the end-to-end suite. run-matrix.sh runs
# it per release; the pipeline's platform jobs run it directly.

set -eu
SRC=${1:-$(cd "$(dirname "$0")/../.." && pwd)}
EL=$(rpm -E %rhel)

cat > /etc/yum.repos.d/trixd-luna2.repo <<EOF
[luna2]
name=luna2 for el${EL}
baseurl=https://updates.clustervision.com/trinityx/16/luna2/rocky/${EL}/x86_64
gpgcheck=0
EOF

# An old minor's own repositories may be gone from the mirrors; luna2 alone is enough then.
dnf -y -q install luna2-python 2>/dev/null \
    || dnf -y -q --disablerepo='*' --enablerepo=luna2 install luna2-python
PY=/trinity/local/python/bin/python3
"$PY" -m pip -q install --disable-pip-version-check -r "$SRC/requirements.txt" \
    pytest httpx setuptools wheel

WORK=$(mktemp -d)
cp -a "$SRC/." "$WORK/"
cd "$WORK"
echo "== $(cat /etc/redhat-release) | python $("$PY" -c 'import platform; print(platform.python_version())')" \
     "| sqlite $("$PY" -c 'import sqlite3; print(sqlite3.sqlite_version)') | $(ldd --version | head -1)"
"$PY" -m pytest -q -p no:cacheprovider
TRIXD_E2E=local "$PY" -m pytest tests/e2e -q -p no:cacheprovider

# A pip upgrade, a forced reinstall and an uninstall leave the site's trixd.ini and its
# database inside the package as they were: only the files the wheel ships are replaced.
WHEELS=$(mktemp -d)
CI_COMMIT_TIMESTAMP=2026-01-01T00:00:00Z "$PY" -m pip wheel -q --no-deps --no-build-isolation -w "$WHEELS/a" .
CI_COMMIT_TIMESTAMP=2026-02-01T00:00:00Z "$PY" -m pip wheel -q --no-deps --no-build-isolation -w "$WHEELS/b" .
"$PY" -m venv --system-site-packages "$WHEELS/venv"
VPY="$WHEELS/venv/bin/python"
"$VPY" -m pip -q install --no-deps "$WHEELS"/a/*.whl
# -I: the installed package, not the checkout in the current directory.
PKG=$("$VPY" -I -c 'import os, trixd; print(os.path.dirname(trixd.__file__))')
mkdir -p "$PKG/var" && echo "[API]" > "$PKG/config/trixd.ini" && echo site-db > "$PKG/var/trixd.db"
"$VPY" -m pip -q install --upgrade --no-deps "$WHEELS"/b/*.whl
"$VPY" -m pip -q install --force-reinstall --no-deps "$WHEELS"/b/*.whl
"$VPY" -m pip -q uninstall -y trixd
test "$(cat "$PKG/config/trixd.ini")" = "[API]" && test "$(cat "$PKG/var/trixd.db")" = site-db
case "$PKG" in "$WHEELS"/*) ;; *) echo "upgrade check looked at $PKG, not the installed package" >&2; exit 1 ;; esac
echo "upgrade check: an upgrade, a forced reinstall and an uninstall kept the site's trixd.ini and database"
