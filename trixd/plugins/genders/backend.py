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
The genders file, from the same node list the slurm blocks are built from.

Luna's own config hooks write this file too, so what is written here has to be what they
write, down to the byte: the markers, the blank line the block opens with and the
administrator's own text around it. tests/unit/golden/ holds the file their generator
produces for the cases in tests/unit/golden_cases.py and the suite holds this against it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure, Conflict
from trixd.core.render.engine import current as renders
from trixd.plugins.slurm.backend import node_set
from trixd.utils import blocks, files

LOGGER = trixlog.get("genders")

# The file this plugin writes and the block in it that is ours. Luna's own writer and the
# pdsh role the installer runs both fix them, so there is nothing here to configure.
WRITES: dict = {
    "genders_file": "/etc/genders",
    "managed_block": "TrinityX",
}
RENDER: str = "genders/renders/genders"
# What gives the file its first block; the render's refusal names it.
INIT: str = "POST /api/v1/genders/files/actions/init"


def genders_block(nodes: list) -> str:
    """
    A line per node: every node is in `all` and in its own group, which is what pdsh
    addresses them by. Each line opens with its own newline, so the block begins with a
    blank line - that line is in the file the other writers leave behind.
    """
    return "".join(f"\n{one['name']} all,{one['group']}" for one in nodes)


class Backend:
    """
    The genders render.
    """

    def config(self) -> dict:
        """
        Where this plugin writes, which the installer fixes.
        """
        return dict(WRITES)

    def _write(self, nodes: list) -> None:
        """
        Our block of the genders file, with everything outside it left as it was found.

        A file carrying no block of ours is the administrator having taken it out to keep a
        genders file of their own, so it is left exactly as it is. /etc/genders is a symlink
        into the shared tree, which is the one file both controllers write, so the link is
        followed rather than refused.
        """
        where = self.config()
        path, manager = where["genders_file"], where["managed_block"]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                held = handle.read()
        except OSError as error:
            raise BackendFailure(f"cannot read {path}: {error}") from None
        wanted = blocks.replace(held, manager, genders_block(nodes))
        if wanted is None:
            raise BackendFailure(f"{path} has no {manager} managed block, so it was left "
                                 f"alone; {INIT} gives it one")
        try:
            blocks.put(path, wanted, follow=True)
        except OSError as error:
            raise BackendFailure(f"cannot write {path}: {error}") from None

    def init(self, body: dict = None) -> dict:
        """
        Give the genders file its managed block at its end, as the installer's role gave
        it, or with `reset` leave nothing but the block, the file as it was kept beside it
        as .bkp; then render. Under the engine's lock, so no render writes in between.
        """
        where = self.config()
        path, manager = where["genders_file"], where["managed_block"]
        reset = bool((body or {}).get("reset"))
        answer = {"file": path}
        with renders().lock:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    held = handle.read()
            except OSError as error:
                raise BackendFailure(f"cannot read {path}: {error}") from None
            if not reset and blocks.block(held, manager) is not None:
                raise Conflict(f"{path} carries the {manager} managed block already, so "
                               f"there is nothing to initialise; `reset` leaves the block "
                               f"alone in it, and the file as it is goes to .bkp")
            try:
                if reset:
                    answer["backup"] = files.write(os.path.realpath(path) + ".bkp", held)
                blocks.put(path, blocks.append("" if reset else held, manager), follow=True)
            except OSError as error:
                raise BackendFailure(f"cannot write {path}: {error}") from None
            LOGGER.info("%s was %s its %s managed block", path,
                        "reset to" if reset else "given", manager)
            item = renders().renders.get(RENDER)
            return {**answer, "rendered": item is not None and renders().run(item)}

    def render_genders(self) -> dict:
        """
        Write the genders file from Luna's nodes.
        """
        fullset = node_set()
        if not fullset:
            LOGGER.info("no nodes yet, so nothing to write")
            return {"nodes": 0, "file": self.config()["genders_file"]}

        # A node Luna holds with no group renders as `<node> all,`, and libgenders refuses an
        # empty attribute for the whole file rather than for the line, so one such node takes
        # pdsh out on every node. Left out and named, as the slurm render does for the same
        # node: the nodes that do have a group stay addressable without it.
        skipped = sorted(one["name"] for one in fullset if not one["group"])
        if skipped:
            LOGGER.error("no group in Luna, so left out of the genders file: %s",
                         ", ".join(skipped))
            fullset = [one for one in fullset if one["group"]]
        if not fullset:
            LOGGER.error("every node Luna holds is without a group, so there is nothing to "
                         "write a genders file from")
            return {"nodes": 0, "file": self.config()["genders_file"], "skipped": skipped}

        self._write(fullset)
        LOGGER.info("wrote the genders file for %d node(s)", len(fullset))
        return {"nodes": len(fullset), "file": self.config()["genders_file"],
                **({"skipped": skipped} if skipped else {})}
