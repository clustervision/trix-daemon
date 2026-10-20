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
requirements.txt states what the shipped code imports, at Luna's versions.

This list goes wrong one way only: a package arrives through another package's
dependencies, gets imported directly, and nobody declares it. It works for exactly as
long as whatever was pulling it in keeps doing so. Both daemons had one - starlette here,
jinja2 in Luna.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import ast
import os
import re
import sys
import warnings

import pytest

import trixd

# A distribution is named one thing and imported as another often enough to matter.
IMPORTED_AS = {"pyjwt": "jwt", "pyyaml": "yaml", "python-pam": "pam"}

# Imported inside a try because a controller may not have them: the installer puts them
# there per cluster, and trixd runs without them. Declaring them would make them
# required, which is the opposite of what they are.
OPTIONAL = {"trinityx_config_slurm", "trinityx_config_genders"}


def test_everything_the_daemon_imports_is_in_requirements():
    """
    Read the imports rather than trusting the file. Scoped to trixd/ - what ships - so a
    test or a tool needing something extra is not a packaging error.
    """
    root = os.path.dirname(trixd.__file__)
    requirements = os.path.join(os.path.dirname(root), "requirements.txt")
    if not os.path.isfile(requirements):
        pytest.skip("installed without its requirements.txt beside it")

    declared = set()
    with open(requirements, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = re.split(r"[=<>!~\[]", line)[0].strip().lower()
            declared.add(IMPORTED_AS.get(name, name.replace("-", "_")))

    missing = {}
    for folder, _, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            source = os.path.join(folder, name)
            with open(source, encoding="utf-8") as handle:
                with warnings.catch_warnings():
                    # Parsing every file surfaces each one's own syntax warnings, which
                    # are nothing to do with what this is checking.
                    warnings.simplefilter("ignore")
                    tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    heads = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    heads = [node.module.split(".")[0]]
                else:
                    continue
                for head in heads:
                    if (head in sys.stdlib_module_names or head == "trixd"
                            or head in declared or head in OPTIONAL):
                        continue
                    missing[head] = os.path.relpath(source, root)

    assert missing == {}, (f"imported but not in requirements.txt: {missing}. Declare it, "
                           f"or import it inside a try and list it in OPTIONAL here.")
