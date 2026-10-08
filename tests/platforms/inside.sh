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
