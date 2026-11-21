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
# On a controller slurmlint comes with the trixd role's slurm helper tasks (and the OOD Slurm
# app); CI installs it itself. The slurm plugin's helper imports it inside a try and answers
# 503 without it.
OPTIONAL = {"slurmlint"}


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
                    beside = os.path.isfile(os.path.join(folder, head + ".py"))  # a script's own module
                    if (head in sys.stdlib_module_names or head == "trixd" or beside
                            or head in declared or head in OPTIONAL):
                        continue
                    missing[head] = os.path.relpath(source, root)

    assert missing == {}, (f"imported but not in requirements.txt: {missing}. Declare it, "
                           f"or import it inside a try and list it in OPTIONAL here.")


# What a controller may not have: the optionals above, plus python-hostlist, which is a third
# party's package and which nothing under trixd/ imports any more. A plugin needing any of them
# to load does not degrade where it is absent, it disappears.
ABSENT = sorted(OPTIONAL | {"hostlist"})

_IMPORTS = """
import importlib
import sys

absent = set(sys.argv[1].split(","))


class Gone:
    \"\"\"Whatever a controller does not have: the import fails as it would there.\"\"\"

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in absent:
            raise ImportError("No module named %r" % name)
        return None


sys.meta_path.insert(0, Gone())
for name in sorted(absent):
    try:
        importlib.import_module(name)
    except ImportError:
        continue
    raise SystemExit("setup: %s imported although it is blocked" % name)
for module in sys.argv[2].split(","):
    importlib.import_module(module)
print("imported")
"""


def test_every_shipped_plugin_imports_without_the_packages_a_controller_may_lack():
    """
    A plugin whose module scope needs one of those packages cannot be imported without it, and
    a plugin that cannot be imported is not served at all - where importing it inside a try
    costs one endpoint. Both sides are derived: the plugins from their manifests, the packages
    from the list above, so a plugin added later is held to this without an edit here.

    It runs in its own interpreter: blocking an import in this one would leave the rest of the
    suite with a second copy of whatever was reimported.
    """
    import glob
    import subprocess

    root = os.path.dirname(trixd.__file__)
    plugins = sorted(os.path.basename(os.path.dirname(path))
                     for path in glob.glob(os.path.join(root, "plugins", "*", "manifest.yaml")))
    assert plugins, "no shipped plugin was found, so this proves nothing"
    modules = [f"trixd.plugins.{name}.backend" for name in plugins]

    done = subprocess.run([sys.executable, "-c", _IMPORTS, ",".join(ABSENT), ",".join(modules)],
                          cwd=os.path.dirname(root), capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": os.path.dirname(root)}, timeout=300)
    assert done.returncode == 0 and done.stdout.strip() == "imported", (
        f"without {', '.join(ABSENT)} a shipped plugin does not import:\n"
        f"{done.stdout}{done.stderr}")
