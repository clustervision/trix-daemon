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

import os

import pytest

from trixd.common.constants import API_PREFIX
from trixd.common.errors import BadRequest, NotFound


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
    from trixd.plugins.directory import backend as directory

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
    from trixd.plugins.directory import backend as directory

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
    from trixd.plugins.directory import backend as directory

    monkeypatch.setattr(directory.shell, "run", lambda *a, **k: Ran("", 1, "no such"))
    with pytest.raises(NotFound):
        directory.Backend().get_user("nobody")


def test_obol_is_never_given_a_shell_string(rig, monkeypatch):
    """
    Every call is an argument list, so a name with a space cannot become a command.
    """
    from trixd.plugins.directory import backend as directory

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
    from trixd.plugins.services import backend as services

    with pytest.raises(NotFound):
        services.Backend().status("sshd")


def test_an_unknown_service_action_is_refused(rig):
    """
    The verb list is closed, so a typo cannot reach the command line.
    """
    from trixd.plugins.services import backend as services

    with pytest.raises(BadRequest):
        services.Backend().act("slurmctld", "obliterate")


def test_the_service_catalogue_round_trips(rig):
    """
    What is written is what comes back, through the declaration store.
    """
    from trixd.plugins.services import backend as services

    backend = services.Backend()
    backend.catalogue(body={"services": {"demo": "demo.service"}})
    assert backend.catalogue()["services"]["demo"] == "demo.service"


def test_the_genders_render_builds_from_the_node_list(rig, monkeypatch):
    """
    The whole node list goes to the generator in one call, and a file without the
    managed block is reported as such rather than as a generic failure.
    """
    from trixd.common.errors import BackendFailure
    from trixd.plugins.genders import backend as genders

    nodes = [{"name": "node001", "group": "compute"}, {"name": "node002", "group": "compute"}]
    given = []
    managed = {"block": True}

    class Generate:
        """
        Stands in for trinityx_config_genders.Generate.
        """

        def Genders(self, fullset):
            """
            Record the call and say whether the file had its block.
            """
            given.append(fullset)
            return managed["block"]

    monkeypatch.setattr(genders, "node_set", lambda: nodes)
    monkeypatch.setattr(genders, "_generator", Generate)
    assert genders.Backend().render_genders() == {"nodes": 2, "file": "/etc/genders"}
    assert given == [nodes]
    managed["block"] = False
    with pytest.raises(BackendFailure, match="no TrinityX managed block"):
        genders.Backend().render_genders()


def test_the_prometheus_targets_render_is_service_discovery(rig, monkeypatch):
    """
    http_sd reads this, so it must be a list of target and label objects.
    """
    from trixd.plugins.prometheus import backend as prometheus

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


def test_the_scaffold_writes_a_plugin_that_loads(rig, tmp_path, client, headers):
    """
    The guide says start from something that works, so it has to actually work.
    """
    from trixd.common.settings import current as settings
    from trixd.core.plugins import loader, scaffold

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
    from trixd.common.errors import Conflict
    from trixd.common.settings import current as settings
    from trixd.core.plugins import scaffold

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
    from trixd.plugins.prometheus import backend as prometheus

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
                              'class="memory",description="DIMM",product="P",vendor="V",'
                              'serial="S1"})')
    assert "serial=" not in infiniband["expr"]
    assert (tmp_path / "trix.hw.node001.cluster.rules").is_file()


def _hw_file(directory, host, group, parts=("memory", "disk")):
    """
    A hardware rule file in Luna's shape, one rule per part.
    """
    import yaml

    rules = [{"alert": "MissingHardwareComponent",
              "expr": f'absent(lshw_device{{hostname="{host}",luna_group="{group}",class="{part}",'
                      f'description="{part} of {host}",serial="S-{part}"}})',
              "labels": {"severity": "warning", "category": "hardware", "nhc": True,
                         "disabled": False},
              "annotations": {"description": "Hardware component {{ $labels.class }} is missing."}}
             for part in parts]
    path = directory / f"trix.hw.{host}.rules"
    path.write_text(yaml.safe_dump({"groups": [{"name": "trinityx_hw", "rules": rules}]}),
                    encoding="utf-8")
    return path


def _prometheus_rig(tmp_path, monkeypatch, nodes):
    """
    A prometheus backend writing to tmp_path, with Luna answering these nodes and no systemctl.
    """
    from types import SimpleNamespace
    from trixd.plugins.prometheus import backend as prometheus

    class Luna:
        """
        Luna, answering the nodes the test gave it.
        """

        def nodes(self):
            """
            Every node.
            """
            return nodes

        def node(self, name):
            """
            One node, or None.
            """
            return nodes.get(name)

    monkeypatch.setattr(prometheus, "luna", lambda: Luna())
    monkeypatch.setattr(prometheus.shell, "run",
                        lambda *args, **kwargs: SimpleNamespace(ok=True, out="", err=""))
    backend = prometheus.Backend()
    backend.config(body={"rules_directory": str(tmp_path)})
    return backend


def test_a_renamed_node_keeps_its_hardware_rules(rig, tmp_path, monkeypatch):
    """
    The file follows the name and every selector names the new host; a near-namesake is untouched.
    """
    import yaml

    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node002": {"hostname": "node002.cluster", "group": "compute"},
                               "node0010": {"hostname": "node0010.cluster", "group": "compute"}})
    _hw_file(tmp_path, "node001.cluster", "compute")
    decoy = _hw_file(tmp_path, "node0010.cluster", "compute")
    before = decoy.read_text(encoding="utf-8")

    outcome = backend.repair_hw_rules("node002", was="node001")

    assert outcome["moved"] == ["trix.hw.node001.cluster.rules"]
    assert not (tmp_path / "trix.hw.node001.cluster.rules").exists()
    moved = (tmp_path / "trix.hw.node002.cluster.rules").read_text(encoding="utf-8")
    document = yaml.safe_load(moved)
    for rule in document["groups"][0]["rules"]:
        assert 'hostname="node002.cluster"' in rule["expr"]
        assert 'hostname="node001.cluster"' not in rule["expr"]
        assert "of node001.cluster" in rule["expr"], "a part label is not a node label"
    assert decoy.read_text(encoding="utf-8") == before


def test_a_rename_luna_already_did_is_finished(rig, tmp_path, monkeypatch):
    """
    Luna's hook renames the file and nothing inside it; the content is repaired in place.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node002": {"hostname": "node002.cluster", "group": "compute"}})
    stale = _hw_file(tmp_path, "node001.cluster", "compute")
    stale.rename(tmp_path / "trix.hw.node002.cluster.rules")

    outcome = backend.repair_hw_rules("node002", was="node001")

    assert outcome["moved"] == [] and outcome["rewritten"] == ["trix.hw.node002.cluster.rules"]
    text = (tmp_path / "trix.hw.node002.cluster.rules").read_text(encoding="utf-8")
    assert 'hostname="node002.cluster"' in text and 'hostname="node001.cluster"' not in text


def test_a_group_change_rewrites_the_selector(rig, tmp_path, monkeypatch):
    """
    luna_group sits in the same selector, so a node moved between groups breaks the same way.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "gpu"}})
    _hw_file(tmp_path, "node001.cluster", "compute")

    outcome = backend.repair_hw_rules("node001")

    assert outcome["rewritten"] == ["trix.hw.node001.cluster.rules"]
    text = (tmp_path / "trix.hw.node001.cluster.rules").read_text(encoding="utf-8")
    assert 'luna_group="gpu"' in text and 'luna_group="compute"' not in text


def test_relabel_touches_only_equality_matchers_on_node_labels():
    """
    A regex matcher, a negation and any other label keep their text to the character.
    """
    from trixd.plugins.prometheus.backend import _relabel

    expr = ('absent(lshw_device{hostname="node001.cluster",hostname!~"node001.*",'
            'luna_group="a",description="hostname=\\"node001.cluster\\""})')
    fixed = _relabel(expr, {"hostname": "node002.cluster", "luna_group": "b"})
    assert fixed == ('absent(lshw_device{hostname="node002.cluster",hostname!~"node001.*",'
                     'luna_group="b",description="hostname=\\"node001.cluster\\""})')


def test_a_deleted_node_has_its_rules_retired(rig, tmp_path, monkeypatch):
    """
    Out of the glob Prometheus loads, kept under backup/ with a timestamp.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    _hw_file(tmp_path, "node009.cluster", "compute")

    outcome = backend.remove_hw_rules("node009")

    assert outcome["retired"] == ["trix.hw.node009.cluster.rules"]
    assert not (tmp_path / "trix.hw.node009.cluster.rules").exists()
    assert list((tmp_path / "backup").glob("trix.hw.node009.cluster.rules.*"))


def test_the_rule_file_render_reconciles_against_luna(rig, tmp_path, monkeypatch):
    """
    Drift is rewritten, a file for a node Luna no longer has is retired, a correct one is untouched.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "gpu"},
                               "node002": {"hostname": "node002.cluster", "group": "compute"}})
    _hw_file(tmp_path, "node001.cluster", "compute")
    right = _hw_file(tmp_path, "node002.cluster", "compute")
    before = right.read_text(encoding="utf-8")
    _hw_file(tmp_path, "node009.cluster", "compute")

    outcome = backend.render_rule_files()

    assert outcome["repaired"] == ["trix.hw.node001.cluster.rules"]
    assert outcome["retired"] == ["trix.hw.node009.cluster.rules"]
    repaired = (tmp_path / "trix.hw.node001.cluster.rules").read_text(encoding="utf-8")
    assert 'luna_group="gpu"' in repaired
    assert right.read_text(encoding="utf-8") == before
    assert not (tmp_path / "trix.hw.node009.cluster.rules").exists()


def test_the_reconcile_removes_nothing_when_luna_lists_no_nodes(rig, tmp_path, monkeypatch):
    """
    An empty answer is not a cluster with no nodes; the files stay exactly as they are.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    kept = _hw_file(tmp_path, "node001.cluster", "compute")
    before = kept.read_text(encoding="utf-8")

    outcome = backend.render_rule_files()

    assert outcome["repaired"] == [] and outcome["retired"] == []
    assert kept.read_text(encoding="utf-8") == before


def test_two_repairs_may_meet_on_one_file(rig, tmp_path, monkeypatch):
    """
    The worker and a render can reach the same file; the one that finds it gone carries on.
    """
    from trixd.plugins.prometheus.backend import _discard

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    _discard(str(tmp_path / "never-there.rules"))
    assert backend._retire(str(tmp_path / "never-there.rules")) == ""
    assert backend.remove_hw_rules("node001") == {"node": "node001", "retired": []}


def test_an_emptied_rule_group_empties_its_file(rig, tmp_path, monkeypatch):
    """
    Taking every service group out leaves the service file empty, not as it was.
    """
    from trixd.plugins.prometheus import backend as prometheus

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    service = os.path.join(str(tmp_path), prometheus.SERVICE_RULES)
    backend.rules(body={"groups": [{"name": "service-a", "rules": []},
                                   {"name": "generic", "rules": []}]})
    assert "service-a" in open(service, encoding="utf-8").read()
    backend.rules(body={"groups": [{"name": "generic", "rules": []}]})
    assert "service-a" not in open(service, encoding="utf-8").read()


def test_a_directory_change_names_the_user_in_the_path_only(rig, client, headers, monkeypatch):
    """
    Changing a shell needs no username in the body: the path already names the user.
    """
    from trixd.plugins.directory import backend as directory

    monkeypatch.setattr(directory.shell, "run", lambda *a, **k: Ran("{}"))
    monkeypatch.setattr(directory.shell, "run_json", lambda *a, **k: {"uid": "carol"})
    answer = client.patch(f"{API_PREFIX}/directory/users/carol", json={"shell": "/bin/zsh"},
                          headers=headers)
    assert answer.status_code == 200, answer.text


def test_a_manager_sets_several_nodes_hardware_rules_in_one_call(rig, tmp_path, monkeypatch):
    """
    What alertx's save-nodes needs: per node, an answer of its own, a node Luna does not
    know refused by name, and Prometheus reloaded once for the lot.
    """
    from types import SimpleNamespace
    from trixd.plugins.prometheus import backend as prometheus

    known = {"node001": {"hostname": "node001.cluster", "group": "compute"},
             "node002": {"hostname": "node002.cluster", "group": "compute"}}
    backend = _prometheus_rig(tmp_path, monkeypatch, known)
    reloads = []
    monkeypatch.setattr(prometheus.shell, "run",
                        lambda *a, **k: reloads.append(a) or SimpleNamespace(ok=True, out="", err=""))

    def query(url, params=None, **_):
        """
        Prometheus, answering one memory part for whichever host is asked about.
        """
        host = params["query"].split('hostname="')[1].split('"')[0]
        result = [{"metric": {"hostname": host, "luna_group": "compute", "class": "memory",
                              "description": "DIMM", "product": "P", "vendor": "V",
                              "serial": "S"}}]
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"result": result}})

    monkeypatch.setattr(prometheus.requests, "get", query)
    answer = backend.set_hw_rules({"nodes": [{"node": "node001", "force": True},
                                             {"node": "node002", "disabled": True},
                                             {"node": "node999"}]})
    by_node = {item["node"]: item for item in answer["nodes"]}
    assert by_node["node001"]["ok"] and by_node["node002"]["ok"]
    assert by_node["node999"] == {"node": "node999", "ok": False,
                                  "message": "not a node Luna knows"}
    assert len(reloads) == 1
    rule = backend.get_hw_rules("node002")["groups"][0]["rules"][0]
    assert rule["labels"]["disabled"] is True


def test_a_node_name_that_could_leave_the_rules_directory_is_refused(rig, tmp_path, monkeypatch):
    """
    The name ends up in a file name, so anything but a plain host name is a 400.
    """
    from trixd.common.errors import BadRequest

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    with pytest.raises(BadRequest):
        backend.set_hw_rules({"nodes": [{"node": "../../etc/passwd"}]})


def test_the_hardware_rules_route_admits_a_manager_and_not_an_operator(rig, client, headers):
    """
    Regenerating rules or switching a node's alerts off is a change, so it needs manager.
    """
    from trixd.core.plugins import manifest as manifests

    path = os.path.join(os.path.dirname(prometheus_module_file()), "manifest.yaml")
    parsed = manifests.parse(manifests.read_yaml(path))
    route = [e for e in parsed.endpoints if e.path == "hw-rules" and "PATCH" in e.methods]
    assert [e.minimum for e in route] == ["manager"]


def prometheus_module_file() -> str:
    """
    Where the shipped prometheus plugin lives.
    """
    from trixd.plugins.prometheus import backend as prometheus
    return prometheus.__file__


def test_a_site_plugin_lives_outside_the_package_and_loads(rig, client, headers):
    """
    A plugin in SITE_DIRECTORY loads beside the shipped ones, and an upgrade, which only
    replaces the package, never touches it.
    """
    from trixd.common.settings import current as settings
    from trixd.core.plugins import loader, scaffold

    site = settings().plugins.site_directory
    scaffold.new("hello", site)
    assert loader.discover()["hello"].startswith(site)
    assert loader.load(rig, "hello").state == "loaded"
    assert client.get(f"{API_PREFIX}/hello", headers=headers).status_code == 200


def test_a_site_plugin_cannot_take_a_shipped_name(rig):
    """
    A site copy of services would replace shipped code unseen, so the shipped one wins.
    """
    from trixd.common.settings import current as settings
    from trixd.core.plugins import loader, scaffold

    site = settings().plugins.site_directory
    os.makedirs(site, exist_ok=True)
    scaffold.new("services", site)
    assert not loader.discover()["services"].startswith(site)


def test_the_scaffold_command_writes_into_the_site_directory(rig, tmp_path, capsys):
    """
    What `trixd.server plugin new` writes has to survive the next upgrade.
    """
    from trixd import server
    from trixd.common.settings import current as settings

    config = settings().config_file
    assert server.main(["--config", config, "plugin", "new", "kept"]) == 0
    assert os.path.isfile(os.path.join(settings().plugins.site_directory, "kept",
                                       "manifest.yaml"))
    assert "[PLUGINS] ENABLED" in capsys.readouterr().out


def test_a_reconcile_with_nothing_to_change_reads_no_file(rig, tmp_path, monkeypatch):
    """
    At a thousand nodes the parse is the whole cost, so an unchanged file under an
    unchanged node is not read again; a node that moved still is, and is repaired.
    """
    from trixd.plugins.prometheus import backend as prometheus

    nodes = {"node001": {"hostname": "node001.cluster", "group": "compute"},
             "node002": {"hostname": "node002.cluster", "group": "compute"}}
    backend = _prometheus_rig(tmp_path, monkeypatch, nodes)
    for name in nodes:
        _hw_file(tmp_path, f"{name}.cluster", "compute")
    read = []
    real = prometheus._read_yaml
    monkeypatch.setattr(prometheus, "_read_yaml", lambda path: read.append(path) or real(path))

    backend.render_rule_files()
    assert len([p for p in read if ".hw." in p]) == 2
    read.clear()
    backend.render_rule_files()
    assert [p for p in read if ".hw." in p] == []

    nodes["node002"]["group"] = "gpu"
    assert backend.render_rule_files()["repaired"] == ["trix.hw.node002.cluster.rules"]
    assert 'luna_group="gpu"' in (tmp_path / "trix.hw.node002.cluster.rules").read_text()
