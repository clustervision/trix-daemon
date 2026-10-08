#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

"""
The repository and the package built from it: house rules for every tracked file,
and a wheel that carries everything the daemon reads at run time.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import glob
import os
import re
import subprocess
import sys
import zipfile

import pytest

from trixd.common.constants import DAEMON_RELEASE

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEADER = "This code is part of the TrinityX software suite"
# Files that are their own kind of text: the licence itself, plain READMEs, git's own.
NO_HEADER = re.compile(r"(^|/)(LICENSE\.txt|README|\.gitattributes)$")


def _tracked() -> list:
    """
    Every file git tracks here, or a skip where there is no git, as in an sdist.
    """
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                text=True, check=True).stdout.split("\n")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [name for name in listed if name]


def _read(name: str) -> bytes:
    """
    One tracked file, as bytes.
    """
    with open(os.path.join(ROOT, name), "rb") as handle:
        return handle.read()


def test_every_tracked_file_is_plain_ascii():
    """
    No typographic dashes, quotes, ellipses or arrows anywhere: they read differently
    in every terminal and editor, and the house style is plain text.
    """
    offending = []
    for name in _tracked():
        for number, line in enumerate(_read(name).decode("utf-8").splitlines(), 1):
            if any(ord(char) > 127 for char in line):
                offending.append(f"{name}:{number}")
    assert not offending, offending


def test_every_tracked_file_carries_the_licence_header():
    """
    The GPL notice at the top of every file but the licence and the plain READMEs.
    """
    missing = [name for name in _tracked() if not NO_HEADER.search(name)
               and HEADER not in _read(name).decode("utf-8")[:2000]]
    assert not missing, missing


def test_the_version_is_the_release_line_and_the_commit_time():
    """
    The same commit gives the same version on every run, so pip sees one version.
    """
    environment = dict(os.environ, CI_COMMIT_TIMESTAMP="2026-10-07T08:00:00Z")
    versions = set()
    for _ in range(2):
        answer = subprocess.run([sys.executable, "setup.py", "--version"], cwd=ROOT,
                                capture_output=True, text=True, env=environment, check=True)
        versions.add(answer.stdout.strip().splitlines()[-1])
    assert versions == {f"{DAEMON_RELEASE}.1791360000"}


def test_the_wheel_carries_every_file_the_daemon_ships(tmp_path):
    """
    Every tracked file under trixd/ is in the wheel, under the name the pipeline expects,
    with the dependencies requirements.txt pins.
    """
    pytest.importorskip("setuptools")
    tracked = [name for name in _tracked() if name.startswith("trixd/")]
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                            "--no-build-isolation", "-q", "-w", str(tmp_path), ROOT],
                           capture_output=True, text=True)
    if built.returncode != 0:
        pytest.skip(f"no wheel could be built here: {built.stderr[-300:]}")

    wheels = glob.glob(str(tmp_path / "*.whl"))
    assert len(wheels) == 1, wheels
    assert re.fullmatch(rf"trixd-{re.escape(DAEMON_RELEASE)}\.\d+-py3-none-any\.whl",
                        os.path.basename(wheels[0])), wheels[0]
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        metadata = wheel.read(next(name for name in names if name.endswith(".dist-info/METADATA")))
    assert not [name for name in tracked if name not in names], "left out of the wheel"

    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as handle:
        pins = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    required = re.findall(r"^Requires-Dist: (.+)$", metadata.decode("utf-8"), re.M)
    normal = {re.sub(r"[\s()]", "", item) for item in required}
    assert normal == set(pins), (normal, pins)
