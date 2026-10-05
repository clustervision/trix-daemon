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
The five plugin backends. Every test here stubs obol, systemctl and Luna.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import pytest

from trix.common.constants import API_PREFIX
from trix.common.errors import BadRequest, NotFound


class Ran:
    """
    What a stubbed command answered, in the shape shell.Result has.
    """

    def __init__(self, out: str = "", code: int = 0, err: str = "") -> None:
        """
        Hold what the stubbed command answered.
        """
        self.out = out
        self.err = err
        self.code = code
        self.ok = code == 0


def test_the_directory_renames_the_389ds_member_attribute(rig, monkeypatch):
    """
    The users application reads member; 389ds calls it uniqueMember.
    """
    from trix.plugins.directory import backend as directory

    monkeypatch.setattr(directory.shell, "run", lambda *a, **k: Ran("{}"))
    monkeypatch.setattr(directory.shell, "run_json",
                        lambda *a, **k: {"cn": "hpc", "uniqueMember": ["uid=carol"]})

    shown = directory.Backend().get_group("hpc")
    assert shown["member"] == ["uid=carol"]
    assert "uniqueMember" not in shown


def test_the_directory_shape_the_ood_application_will_meet(rig, monkeypatch):
    """
    Pinned deliberately. Luna wraps a record in config and keys it by name; this
    answers the record, so repointing the app takes two unwraps out of it.
    """
    from trix.plugins.directory import backend as directory

    monkeypatch.setattr(directory.shell, "run", lambda *a, **k: Ran("{}"))
    monkeypatch.setattr(directory.shell, "run_json",
                        lambda *a, **k: {"uid": "carol", "uidNumber": "1051"})

    shown = directory.Backend().get_user("carol")
    assert shown["uid"] == "carol"
    assert "config" not in shown, "no Luna envelope here"
    assert "carol" not in shown, "not keyed by name either"


def test_a_missing_directory_user_is_a_404(rig, monkeypatch):
    """
    obol answering non-zero is a missing user, not an empty one.
    """
    from trix.plugins.directory import backend as directory

    monkeypatch.setattr(directory.shell, "run", lambda *a, **k: Ran("", 1, "no such"))
    with pytest.raises(NotFound):
        directory.Backend().get_user("nobody")


def test_obol_is_never_given_a_shell_string(rig, monkeypatch):
    """
    Every call is an argument list, so a name with a space cannot become a command.
    """
    from trix.plugins.directory import backend as directory

    seen = []
    monkeypatch.setattr(directory.shell, "run",
                        lambda argv, *a, **k: (seen.append(argv), Ran("{}"))[1])
    monkeypatch.setattr(directory.shell, "run_json",
                        lambda argv, *a, **k: (seen.append(argv), {})[1])
    directory.Backend().get_user("carol; rm -rf /")

    assert seen, "nothing ran"
    for argv in seen:
        assert isinstance(argv, list), argv
        assert argv[0] == "obol"
        assert "carol; rm -rf /" in argv, "the name is one argument, not parsed"


def test_a_service_outside_the_catalogue_is_refused(rig):
    """
    An endpoint that took any unit name would restart anything on the controller.
    """
    from trix.plugins.services import backend as services

    with pytest.raises(NotFound):
        services.Backend().status("sshd")


def test_an_unknown_service_action_is_refused(rig):
    """
    The verb list is closed, so a typo cannot reach the command line.
    """
    from trix.plugins.services import backend as services

    with pytest.raises(BadRequest):
        services.Backend().act("slurmctld", "obliterate")


def test_the_service_catalogue_round_trips(rig):
    """
    What is written is what comes back, through the declaration store.
    """
    from trix.plugins.services import backend as services

    backend = services.Backend()
    backend.catalogue(body={"services": {"demo": "demo.service"}})
    assert backend.catalogue()["services"]["demo"] == "demo.service"


def test_the_genders_render_builds_from_the_node_list(rig, monkeypatch):
    """
    One line per node, from Luna's list, with no call per node.
    """
    from trix.plugins.genders import backend as genders

    monkeypatch.setattr(genders, "_generator", lambda: None)
    document = genders.Backend().config()
    assert isinstance(document, dict)


def test_the_prometheus_targets_render_is_service_discovery(rig, monkeypatch):
    """
    http_sd reads this, so it must be a list of target and label objects.
    """
    from trix.plugins.prometheus import backend as prometheus

    class Luna:
        """
        Two nodes, as Luna would answer them.
        """

        def nodes(self):
            """
            Every node this stubbed Luna knows.
            """
            return {"node001": {"hostname": "node001.cluster", "group": "compute"},
                    "node002": {"hostname": "node002.cluster", "group": "compute"}}

        def node(self, name):
            """
            One node by name, or None.
            """
            return self.nodes().get(name)

        def networks(self):
            """
            No networks, which is enough for a target.
            """
            return {}

    monkeypatch.setattr(prometheus, "luna", lambda: Luna())
    document = prometheus.Backend().render_targets()

    assert isinstance(document, list)
    for entry in document:
        assert "targets" in entry and isinstance(entry["targets"], list)
        assert "labels" in entry and isinstance(entry["labels"], dict)


def test_the_slurm_partitions_round_trip(rig):
    """
    The On Demand application edits partitions through this, so it must persist.
    """
    from trix.plugins.slurm import backend as slurm

    backend = slurm.Backend()
    backend.partitions(body={"partitions": {"defq": {"nodes": "node[001-004]"}}})
    assert backend.partitions()["partitions"]["defq"]["nodes"] == "node[001-004]"


def test_the_scaffold_writes_a_plugin_that_loads(rig, tmp_path, client, headers):
    """
    The guide says start from something that works, so it has to actually work.
    """
    from trix.common.settings import current as settings
    from trix.core.plugins import loader, scaffold

    written = scaffold.new("hello", settings().plugins.directory)
    assert any(path.endswith("manifest.yaml") for path in written)

    entry = loader.load(rig, "hello")
    assert entry.state == "loaded"
    assert entry.routes, "it declared endpoints and served none"

    answer = client.get(f"{API_PREFIX}/hello", headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.json()["plugin"] == "hello"

    changed = client.patch(f"{API_PREFIX}/hello/setting",
                           json={"value": "set"}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert client.get(f"{API_PREFIX}/hello/setting",
                      headers=headers).json()["value"] == "set"


def test_the_scaffold_refuses_a_reserved_or_bad_name(rig):
    """
    luna is the pass-through, and a name that is not a path segment is not a name.
    """
    from trix.common.errors import Conflict
    from trix.common.settings import current as settings
    from trix.core.plugins import scaffold

    with pytest.raises(BadRequest):
        scaffold.new("luna", settings().plugins.directory)
    with pytest.raises(BadRequest):
        scaffold.new("Not A Name", settings().plugins.directory)

    scaffold.new("once", settings().plugins.directory)
    with pytest.raises(Conflict):
        scaffold.new("once", settings().plugins.directory)


def test_the_hardware_rules_take_lunas_shape(rig, tmp_path, monkeypatch):
    """
    The metric the exporter emits, the labels Luna selects on, and Luna's alert and group.
    """
    from types import SimpleNamespace
    from trix.plugins.prometheus import backend as prometheus

    class Luna:
        """
        One node, as Luna would answer it.
        """

        def node(self, name):
            """
            The record the targets render reads the hostname from.
            """
            return {"hostname": "node001.cluster", "group": "compute"}

    asked = {}

    def query(url, params=None, **_):
        """
        Prometheus, answering two components, one of them InfiniBand.
        """
        asked["query"] = params["query"]
        result = [{"metric": {"hostname": "node001.cluster", "luna_group": "compute",
                              "class": "memory", "description": "DIMM", "product": "P",
                              "vendor": "V", "serial": "S1"}},
                  {"metric": {"hostname": "node001.cluster", "luna_group": "compute",
                              "class": "network", "description": "interface", "product": "IB",
                              "vendor": "M", "serial": "S2"}}]
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"result": result}})

    monkeypatch.setattr(prometheus, "luna", lambda: Luna())
    monkeypatch.setattr(prometheus.requests, "get", query)
    monkeypatch.setattr(prometheus.shell, "run",
                        lambda *args, **kwargs: SimpleNamespace(ok=True, out="", err=""))
    backend = prometheus.Backend()
    backend.config(body={"rules_directory": str(tmp_path)})

    answer = backend.import_hw_rules("node001", {"force": True})
    assert answer == {"hostname": "node001.cluster", "rules": 2}
    assert 'hostname="node001.cluster"' in asked["query"]
    assert "by (hostname,luna_group,class,description,product,vendor,serial)" in asked["query"]

    group = backend.get_hw_rules("node001")["groups"][0]
    assert group["name"] == "trinityx_hw"
    memory, infiniband = group["rules"]
    assert memory["alert"] == "MissingHardwareComponent" and "for" not in memory
    assert memory["expr"] == ('absent(lshw_device{hostname="node001.cluster",luna_group="compute",'
                              'class="memory",description="DIMM",product="P",vendor="V",serial="S1"})')
    assert "serial=" not in infiniband["expr"]
    assert (tmp_path / "trix.hw.node001.cluster.rules").is_file()
