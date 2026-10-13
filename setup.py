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
Builds the trixd pip package.

Every field is declared here, not in pyproject.toml: the shared pip pipeline runs
"python setup.py sdist bdist_wheel" on setuptools 57, which does not read a
pyproject [project] table and would build a package called UNKNOWN.

The version is the release line, DAEMON_RELEASE in trixd/common/constants.py,
plus the commit's time, as luna2-daemon's is its release plus a time: every build is newer than the one before it, so "pip install
--upgrade" takes it, and the pipeline's pruning, which sorts by name, keeps the
newest. It is the commit's time rather than the clock's so that pip, which runs
this file more than once per install, sees the same version every time.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import re
import subprocess
from datetime import datetime

from setuptools import find_namespace_packages, setup

HERE = os.path.dirname(os.path.abspath(__file__))


def release() -> str:
    """
    The release line, read rather than imported: importing the package needs its
    dependencies, which a build does not have.
    """
    with open(os.path.join(HERE, "trixd", "common", "constants.py"), encoding="utf-8") as handle:
        return re.search(r'^DAEMON_RELEASE: str = "([^"]+)"', handle.read(), re.M).group(1)


def build_number() -> int:
    """
    The commit's time in seconds, from the first of these that has it.

    PKG-INFO is there when an sdist is being built into a wheel, and carries the
    number the sdist was made with. CI_COMMIT_TIMESTAMP is set by GitLab, whose
    build image has no git. git answers in a checkout. 0 when none does.
    """
    try:
        with open(os.path.join(HERE, "PKG-INFO"), encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Version:"):
                    return int(line.split(":", 1)[1].strip().rsplit(".", 1)[1])
    except (OSError, ValueError, IndexError):
        pass
    stamp = os.environ.get("CI_COMMIT_TIMESTAMP", "")
    if stamp:
        # Python 3.10 does not read a trailing Z.
        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=HERE,
                             capture_output=True, text=True, check=True).stdout
        return int(out.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0


def requirements() -> list:
    """
    The pins in requirements.txt, so they are declared once.
    """
    with open(os.path.join(HERE, "requirements.txt"), encoding="utf-8") as handle:
        lines = [line.strip() for line in handle]
    return [line for line in lines if line and not line.startswith("#")]


def readme() -> str:
    """
    The README, as the package's long description.
    """
    with open(os.path.join(HERE, "README.md"), encoding="utf-8") as handle:
        return handle.read()


setup(
    name="trixd",
    version=f"{release()}.{build_number()}",
    description="TrinityX control-plane daemon for everything that is not a physical node",
    long_description=readme(),
    long_description_content_type="text/markdown",
    author="ClusterVision Development team",
    author_email="support@clustervision.com",
    maintainer="ClusterVision Development team",
    maintainer_email="support@clustervision.com",
    url="https://gitlab.taurusgroup.one/clustervision/trix-daemon",
    license="GPL-3.0-or-later",
    keywords=["trinityx", "trixd", "daemon", "clustervision"],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3.10",
    ],
    python_requires=">=3.10",
    install_requires=requirements(),
    # Namespace packages too: the migrations directories hold modules but no __init__.py.
    packages=find_namespace_packages(include=["trixd", "trixd.*"], exclude=["*.__pycache__"]),
    package_data={"trixd": ["config/*.sample", "config/*.service", "config/*.yaml",
                           "plugins/*/manifest.yaml", "core/security/sources/README",
                           "db/migrations/script.py.mako",
                           "static/*.txt", "static/*.css", "static/*.png", "static/*.ico",
                           "static/vendor/swagger-ui/*"]},
    entry_points={"console_scripts": ["trixd = trixd.server:main"]},
    zip_safe=False,
)
