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
Time the prometheus plugin against many nodes: the targets render, the rule-file
reconcile, and the rename hook. Luna and systemctl are stood in for; the files,
the database and the plugin code are real.

    python tests/perf/thousand_nodes.py [nodes] [parts per node]
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from trix.common import settings as configuration  # noqa: E402
from trix.db import engine as database  # noqa: E402
from trix.plugins.prometheus import backend as prometheus  # noqa: E402

GROUPS: int = 8


class Luna:
    """
    Luna answering a fixed estate of nodes, as the real client would.
    """

    def __init__(self, count: int) -> None:
        """
        count nodes over eight groups, each provisioning over BOOTIF.
        """
        self.records = {}
        for index in range(count):
            name = f"node{index:04d}"
            self.records[name] = {
                "hostname": f"{name}.cluster", "group": f"group{index % GROUPS}",
                "provision_interface": "BOOTIF",
                "interfaces": [{"interface": "BOOTIF",
                                "ipaddress": f"10.141.{index // 250}.{index % 250 + 1}"}]}

    def nodes(self) -> dict:
        """
        Every node, as config/node answers.
        """
        return self.records

    def node(self, name: str) -> dict:
        """
        One node, or None.
        """
        return self.records.get(name)


def rule_file(directory: str, host: str, group: str, parts: int) -> None:
    """
    A hardware rule file in the shape the plugin writes, one rule per part.
    """
    rules = [{"alert": "MissingHardwareComponent",
              "expr": f'absent(lshw_device{{hostname="{host}",luna_group="{group}",'
                      f'class="{part}",description="{part} of {host}",serial="S-{part}"}})',
              "labels": {"severity": "warning", "category": "hardware", "nhc": True,
                         "disabled": False}}
             for part in (f"part{index}" for index in range(parts))]
    with open(os.path.join(directory, f"trix.hw.{host}.rules"), "w", encoding="utf-8") as handle:
        yaml.safe_dump({"groups": [{"name": "trinityx_hw", "rules": rules}]}, handle)


def timed(action, repeat: int = 1) -> float:
    """
    The median wall time of an action, in milliseconds.
    """
    spent = []
    for _ in range(repeat):
        started = time.perf_counter()
        action()
        spent.append((time.perf_counter() - started) * 1000)
    return statistics.median(spent)


def main(count: int, parts: int) -> None:
    """
    Build the estate, then time each piece of work and print one line each.
    """
    scratch = tempfile.mkdtemp(prefix="trix-perf-")
    try:
        ini = os.path.join(scratch, "trixd.ini")
        with open(ini, "w", encoding="utf-8") as handle:
            handle.write(f"[LOGGER]\nLEVEL = error\nLOGFILE =\n"
                         f"[DATABASE]\nURL = sqlite:///{scratch}/trix.db\n")
        configuration.set_current(configuration.load(ini))
        database.start(configuration.current().database.url)

        luna = Luna(count)
        prometheus.luna = lambda: luna
        prometheus.shell.run = lambda *a, **k: SimpleNamespace(ok=True, out="", err="")
        rules = os.path.join(scratch, "rules")
        os.makedirs(rules)
        backend = prometheus.Backend()
        backend.config(body={"rules_directory": rules})
        for name, node in luna.records.items():
            rule_file(rules, node["hostname"], node["group"], parts)

        targets = []
        results = [("targets render", timed(lambda: targets.extend(backend.render_targets()), 5))]
        size = len(json.dumps(backend.render_targets()))
        results.append(("rule-file reconcile, first pass", timed(backend.render_rule_files)))
        results.append(("rule-file reconcile, nothing changed since",
                        timed(backend.render_rule_files, 3)))

        moved = list(luna.records)[: count // 10]
        for name in moved:
            luna.records[name]["group"] = "elsewhere"
        results.append((f"rule-file reconcile, {len(moved)} nodes moved group",
                        timed(backend.render_rule_files)))

        first = moved[0]

        def rename_there_and_back() -> None:
            """
            One rename through the hook, then back, as two passes would see them.
            """
            record = luna.records.pop(first)
            luna.records[first + "r"] = dict(record, hostname=f"{first}r.cluster")
            backend.node_updated(first, newnodename=first + "r")
            luna.records[first] = luna.records.pop(first + "r")
            luna.records[first]["hostname"] = f"{first}.cluster"
            backend.node_updated(first + "r", newnodename=first)

        results.append(("rename hook, one node there and back", timed(rename_there_and_back, 5)))

        print(f"{count} nodes, {parts} rules per node, {count * parts} hardware rules, "
              f"Python {platform.python_version()} on {platform.node()}, "
              f"libyaml {'yes' if prometheus.SAFE_LOADER is not yaml.SafeLoader else 'no'}")
        for label, milliseconds in results:
            print(f"  {label:44} {milliseconds:9.1f} ms")
        print(f"  {'targets document':44} {size / 1024:9.1f} KiB, {len(targets) // 5} entries")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000,
         int(sys.argv[2]) if len(sys.argv) > 2 else 8)
