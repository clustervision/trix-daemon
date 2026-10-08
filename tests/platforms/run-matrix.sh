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

# The unit tests and the end-to-end suite on each EL release trixd supports, each in a
# container of its own on the luna2-python it runs on. Needs podman or docker, and the
# internet. IMAGES overrides the list.
#
#     bash tests/platforms/run-matrix.sh

set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
IMAGES=${IMAGES:-"quay.io/rockylinux/rockylinux:8.4 docker.io/rockylinux/rockylinux:8.10
                  docker.io/rockylinux/rockylinux:9.0 docker.io/rockylinux/rockylinux:9
                  docker.io/rockylinux/rockylinux:10 docker.io/library/almalinux:10"}
ENGINE=$(command -v podman || command -v docker)
[ -n "$ENGINE" ] || { echo "needs podman or docker" >&2; exit 2; }

FAILED=""
for image in $IMAGES; do
    echo "#### $image"
    if ! "$ENGINE" run --rm -v "$SRC":/src:ro,Z "$image" bash /src/tests/platforms/inside.sh /src; then
        FAILED="$FAILED $image"
    fi
done
if [ -n "$FAILED" ]; then
    echo "failed on:$FAILED"
    exit 1
fi
echo "passed on every release"
