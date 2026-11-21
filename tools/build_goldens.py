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
Write tests/unit/golden/ from the packaged generators: for every case in
tests/unit/golden_cases.py, the file as the administrator left it, handed to
trinityx-config-slurm and trinityx-config-genders exactly as a render hands it to them,
and kept as they left it.

It also reads the table's `NODESETS` rows back off the slurm generator's own hostlist
filter, so a fold that has moved is a refusal here rather than a mismatch nobody can
account for.

Those packages are not on PyPI and are not in requirements.txt, so this runs on a
controller that has them and never in the pipeline; the files it writes are committed, and
test_golden_files.py reads them. Run it when the table changes, or to bless a generator
that legitimately writes something new:

    python tools/build_goldens.py
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import importlib.util
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GENERATORS = {"trinityx_config_slurm": "trinityx-config-slurm",
              "trinityx_config_genders": "trinityx-config-genders"}


def cases():
    """
    The table, loaded by path as the tests load it.
    """
    path = os.path.join(ROOT, "tests", "unit", "golden_cases.py")
    spec = importlib.util.spec_from_file_location("golden_cases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generators() -> tuple:
    """
    Both packages, or a named refusal. A missing one is the whole point of this being a
    tool rather than a test: there is nothing to fall back to and nothing to skip.
    """
    missing = [name for module, name in GENERATORS.items()
               if importlib.util.find_spec(module) is None]
    if missing:
        raise SystemExit(f"{', '.join(missing)} is not installed here, so the golden files "
                         f"cannot be built. Run this on a controller that has them")
    from trinityx_config_genders import Generate as Genders
    from trinityx_config_slurm import Generate as Slurm
    return Slurm(), Genders()


def nodesets(table) -> None:
    """
    The table's declared folds against the generator's own filter. The rows are committed
    rather than written, so this says which have drifted rather than quietly moving them.
    """
    from trinityx_config_slurm.trinityx_config_slurm import Templating

    moved = [(names, folded, Templating.jinja_compress(",".join(names)))
             for names, folded in table.NODESETS
             if Templating.jinja_compress(",".join(names)) != folded]
    if moved:
        raise SystemExit("\n".join(f"{list(names)}: the table says {folded!r} and the "
                                   f"generator answers {theirs!r}"
                                   for names, folded, theirs in moved))


def build(table, slurm, genders) -> list:
    """
    One directory per case, each file written as the administrator left it and then handed
    to the generator that owns it. They write in place, so what is left at the path is the
    golden.
    """
    written = []
    for case in table.CASES:
        folder = os.path.join(table.GOLDEN, case["case"])
        os.makedirs(folder, exist_ok=True)
        paths = {kind: table.golden(case, kind) for kind in table.KINDS}
        for kind, path in paths.items():
            with open(path, "w", encoding="utf-8", newline=chr(10)) as handle:
                handle.write(table.before(case, kind))
        if not slurm.all_configs(nodes=case["nodes"], manager=table.MANAGER,
                                 configs={k: v for k, v in paths.items() if k != "genders"}):
            raise SystemExit(f"{case['case']}: the slurm generator reported a failure")
        wrote = genders.Custom("genders.j2", paths["genders"], case["nodes"])
        if wrote is not case.get("managed", True):
            raise SystemExit(f"{case['case']}: the genders generator answered {wrote}, and "
                             f"the case says its file holds a managed block or does not")
        written += sorted(paths.values())
    return written


if __name__ == "__main__":
    table = cases()
    slurm, genders = generators()
    nodesets(table)
    # Whatever is there is replaced, so a case taken out of the table takes its files with
    # it rather than leaving a golden nothing reads any more.
    shutil.rmtree(table.GOLDEN, ignore_errors=True)
    for path in build(table, slurm, genders):
        print(os.path.getsize(path), os.path.relpath(path, ROOT))
