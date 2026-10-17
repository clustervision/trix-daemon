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
from trixd.utils import files


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
    The daemon's own prefixes are reserved, a shipped plugin's name is taken, and a name
    that is not a path segment is not a name.
    """
    from trixd.common.errors import Conflict
    from trixd.common.settings import current as settings
    from trixd.core.plugins import scaffold

    for reserved in ("auth", "system", "identity", "docs"):
        with pytest.raises(BadRequest):
            scaffold.new(reserved, settings().plugins.directory)
    for shipped in ("luna", "prometheus"):
        with pytest.raises(Conflict, match="shipped plugin"):
            scaffold.new(shipped, str(settings().plugins.site_directory),
                         settings().plugins.directory)
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
    assert answer == {"hostname": "node001.cluster", "rules": 2, "changed": True}
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


def test_the_targets_render_keeps_what_it_served_when_luna_lists_no_nodes(
        rig, tmp_path, monkeypatch):
    """
    The same rule the reconcile above follows, for the document Prometheus discovers from:
    an empty answer is not a cluster with no nodes, and a document without them stops every
    node metric at once. The render refuses, and what was already served stays.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    nodes = {"node001": {"hostname": "node001.cluster", "group": "compute"}}
    backend = _prometheus_rig(tmp_path, monkeypatch, nodes)
    item = renders().register("prometheus", "renders/targets",
                              Render(inputs=["luna.node"], coalesce="1s"),
                              backend.render_targets)
    try:
        assert renders().run(item) is True, "the first render produced nothing"
        served = item.document
        assert [entry for entry in served if "node001.cluster" in entry["targets"][0]], \
            "node001 was never in the document, so emptying it would prove nothing"

        nodes.clear()

        assert renders().run(item) is False, "a document with no nodes in it was produced"
        assert item.document == served, "the targets already served were replaced"
    finally:
        renders().forget("prometheus")


def test_forgetting_an_exporter_name_too_long_for_the_column_is_refused(rig, tmp_path,
                                                                        monkeypatch):
    """
    The write path refuses it; answering 200 to the same name only because this is the
    delete would say the request was understood.
    """
    from trixd.common.errors import BadRequest

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    with pytest.raises(BadRequest):
        backend.remove_exporter("e" * 256)
    with pytest.raises(BadRequest):
        backend.remove_applied("")


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


def test_a_manifest_that_could_not_work_is_refused_at_load(rig):
    """
    Four shapes that were accepted and then went wrong somewhere the author would not look.

    A duplicate path and verb: Starlette matches the first, so the second handler is dead
    code its author believes is serving. A render with a path variable or a query parameter:
    the reader takes no arguments, so one document answers for every value and the producer
    is never told which was asked for. A node endpoint whose path carries none of the names
    the gate binds to: the binding is skipped in silence and one node's token is good for
    another node's data. A path variable called body, caller or request: the handler is
    handed the path's value where it expects the caller and answers 500 on its first call.

    Each was reachable only through the command-line check, which a reload does not run, or
    through nothing at all.
    """
    from trixd.common.errors import PluginRefused
    from trixd.core.plugins import manifest as manifests

    base = {"plugin": "probe", "backend": "backend.Backend"}
    for endpoints, because in (
            ([{"path": "renders/thing/<network>", "methods": ["GET"], "minimum": "none",
               "handler": "render_thing",
               "render": {"inputs": ["luna.node"], "serve": "json"}}], "path variable"),
            ([{"path": "renders/thing", "methods": ["GET"], "minimum": "none",
               "handler": "render_thing", "query": {"since": {"type": "string"}},
               "render": {"inputs": ["luna.node"], "serve": "json"}}], "query parameter"),
            ([{"path": "rules/<thing>", "methods": ["PATCH"], "handler": "write",
               "token": "node"}], "node token"),
            ([{"path": "metrics/<caller>", "methods": ["GET"], "handler": "read",
               "public": True}], "path variable caller")):
        with pytest.raises(PluginRefused) as refusal:
            manifests.parse({**base, "endpoints": endpoints})
        assert because in str(refusal.value), (because, str(refusal.value))

    # And the shapes that must still load, so this cannot pass by refusing everything: a
    # node endpoint that does carry the variable, and a render with neither.
    sound = manifests.parse({**base, "endpoints": [
        {"path": "rules/<node>", "methods": ["PATCH"], "handler": "write", "token": "node"},
        {"path": "renders/thing", "methods": ["GET"], "minimum": "none",
         "handler": "render_thing",
         "render": {"inputs": ["luna.node"], "serve": "json"}},
        {"path": "nodes", "methods": ["GET", "POST"], "handler": "nodes",
         "minimum": "manager"}]})
    assert manifests.duplicates(sound) == [], "one path with two verbs is not a duplicate"

    # A duplicate is collected rather than raised, because the command-line check lists
    # every problem it can find in one run and a refusal from the parse ends that at the
    # first one. The load path refuses on what it collects, which is the next test.
    twice = manifests.parse({**base, "endpoints": [
        {"path": "nodes", "methods": ["POST"], "handler": "create", "minimum": "manager"},
        {"path": "nodes", "methods": ["POST"], "handler": "replace", "minimum": "manager"}]})
    assert manifests.duplicates(twice) == ["POST nodes is declared twice"], \
        manifests.duplicates(twice)


def test_loading_a_plugin_that_declares_a_verb_twice_is_refused(rig, tmp_path):
    """
    Starlette matches the first of two routes with the same path and verb, so the second
    handler is dead code its author believes is serving. Only the command-line check found
    it, and a reload does not run that - so the way this actually reaches anyone is a
    reload that says it loaded.
    """
    from trixd.common.errors import PluginRefused
    from trixd.core.plugins import loader

    plugin = tmp_path / "plugins" / "twofold"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: twofold\nbackend: backend\nendpoints:\n"
        "  - path: nodes\n    methods: [POST]\n    minimum: manager\n    handler: create\n"
        "  - path: nodes\n    methods: [POST]\n    minimum: manager\n    handler: replace\n",
        encoding="utf-8")
    (plugin / "backend.py").write_text(
        'class Backend:\n    """Two handlers for one route."""\n\n'
        '    def create(self, body: dict = None):\n        """One."""\n        return {}\n\n'
        '    def replace(self, body: dict = None):\n        """The other."""\n        return {}\n',
        encoding="utf-8")

    with pytest.raises(PluginRefused, match="declared twice"):
        loader.load(rig, "twofold")


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


def test_the_luna_plugin_forwards_as_sent_and_luna_decides(rig, client, monkeypatch):
    """
    No token check here: the caller's own header goes to the Luna luna.ini names.
    """
    from types import SimpleNamespace

    seen = {}

    def luna(method, url, **kwargs):
        """
        Luna, answering whatever it was sent.
        """
        seen.update(method=method, url=url, headers=kwargs["headers"])
        return SimpleNamespace(content=b'{"ok": true}', status_code=200,
                               headers={"content-type": "application/json"})

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request", luna)
    answer = client.get(f"{API_PREFIX}/luna/config/node?a=1&a=2",
                        headers={"x-access-tokens": "a-token-only-luna-judges"})
    assert answer.status_code == 200 and answer.json() == {"ok": True}
    assert seen["url"] == "https://luna.test:7050/config/node?a=1&a=2"
    assert seen["headers"]["x-access-tokens"] == "a-token-only-luna-judges"


def test_unplugging_the_luna_plugin_removes_the_route_to_luna(rig, client, headers):
    """
    The pass-through is a plugin like any other: unplugged, /api/v1/luna is gone.
    """
    assert client.post(f"{API_PREFIX}/system/plugins/luna/disable",
                       headers=headers).status_code == 200
    assert client.get(f"{API_PREFIX}/luna/config/node", headers=headers).status_code == 404
    assert client.post(f"{API_PREFIX}/system/plugins/luna/reload",
                       headers=headers).status_code == 200


def _hw_backend(tmp_path, monkeypatch, reload_ok=True):
    """
    A prometheus backend over tmp_path with one node, Prometheus answering one DIMM, and a
    reload that is counted.
    """
    from types import SimpleNamespace

    from trixd.plugins.prometheus import backend as prometheus

    class Luna:
        """
        One node.
        """

        def node(self, name):
            """
            Its record.
            """
            return {"hostname": "node001.cluster", "group": "compute"}

    def query(url, params=None, **_):
        """
        One component.
        """
        result = [{"metric": {"hostname": "node001.cluster", "luna_group": "compute",
                              "class": "memory", "description": "DIMM", "product": "P",
                              "vendor": "V", "serial": "S1"}}]
        return SimpleNamespace(status_code=200, text="", json=lambda: {"data": {"result": result}})

    reloads = []

    def reload(argv, **kwargs):
        """
        systemctl reload prometheus-server, counted.
        """
        reloads.append(argv)
        return SimpleNamespace(ok=reload_ok, out="", err="" if reload_ok else "unit failed")

    monkeypatch.setattr(prometheus, "luna", lambda: Luna())
    monkeypatch.setattr(prometheus.requests, "get", query)
    monkeypatch.setattr(prometheus.shell, "run", reload)
    monkeypatch.setattr(prometheus, "RELOADS", prometheus.SharedReload())
    backend = prometheus.Backend()
    backend.config(body={"rules_directory": str(tmp_path)})
    return backend, reloads


def test_prometheus_is_asked_over_https_by_default(rig):
    """
    TrinityX serves Prometheus over TLS, so that is the default address.
    """
    from trixd.plugins.prometheus import backend as prometheus

    assert prometheus.DEFAULT_CONFIG["prometheus_url"] == "https://localhost:9090"


def test_a_scheme_mismatch_is_found_and_remembered(rig, tmp_path, monkeypatch):
    """
    https against a plain Prometheus, or http against a TLS one, is retried once on the
    other scheme, and that scheme is kept.
    """
    from types import SimpleNamespace

    import requests

    from trixd.plugins.prometheus import backend as prometheus

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    asked = []

    def plain_only(url, **_):
        """
        A Prometheus without TLS.
        """
        asked.append(url)
        if url.startswith("https://"):
            raise requests.exceptions.SSLError("wrong version number")
        return SimpleNamespace(status_code=200, text="", json=lambda: {"data": {"result": []}})

    monkeypatch.setattr(prometheus.requests, "get", plain_only)
    backend._query("up")
    backend._query("up")
    assert asked[0].startswith("https://") and asked[1].startswith("http://")
    assert asked[2].startswith("http://"), "the working scheme is remembered"

    backend.prometheus_url = ""
    backend.config(body={"prometheus_url": "http://localhost:9090"})
    asked.clear()

    def tls_only(url, **_):
        """
        A TLS Prometheus asked over http, as on a TrinityX controller.
        """
        asked.append(url)
        if url.startswith("http://"):
            return SimpleNamespace(status_code=400,
                                   text="Client sent an HTTP request to an HTTPS server.")
        return SimpleNamespace(status_code=200, text="", json=lambda: {"data": {"result": []}})

    monkeypatch.setattr(prometheus.requests, "get", tls_only)
    backend._query("up")
    assert asked == [asked[0], asked[1]] and asked[1].startswith("https://")


def test_a_node_that_rebooted_unchanged_costs_no_write_and_no_reload(rig, tmp_path,
                                                                     monkeypatch):
    """
    The boot-storm case: the node hook asks again after every boot, and nothing changed.
    """
    backend, reloads = _hw_backend(tmp_path, monkeypatch)
    first = backend.import_hw_rules("node001", {"force": False})
    assert first["changed"] is True and len(reloads) == 1
    path = tmp_path / "trix.hw.node001.cluster.rules"
    stamp = path.stat().st_mtime_ns
    for _ in range(50):
        again = backend.import_hw_rules("node001", {"force": False})
        assert again["changed"] is False
    assert len(reloads) == 1, "no reload for an unchanged node"
    assert path.stat().st_mtime_ns == stamp, "the file was not rewritten"


def test_a_changed_rule_file_is_backed_up_before_it_is_overwritten(rig, tmp_path, monkeypatch):
    """
    As Luna does: the old file goes to backup/ with a timestamp, then the new one is written.
    """
    backend, reloads = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    answer = backend.set_hw_rules({"nodes": [{"node": "node001", "nhc": False}]})
    assert answer["nodes"][0]["changed"] is True
    kept = list((tmp_path / "backup").glob("trix.hw.node001.cluster.rules.*"))
    assert len(kept) == 1
    assert "nhc: true" in kept[0].read_text(encoding="utf-8")
    assert len(reloads) == 2


def test_a_failed_reload_is_a_502_that_says_so(rig, tmp_path, monkeypatch):
    """
    Luna fails the import when Prometheus does not reload; so does trixd, with the reason.
    """
    from trixd.common.errors import BackendFailure

    backend, _ = _hw_backend(tmp_path, monkeypatch, reload_ok=False)
    with pytest.raises(BackendFailure, match="did not reload: unit failed") as raised:
        backend.import_hw_rules("node001", {})
    assert raised.value.status_code == 502
    with pytest.raises(BackendFailure, match="1 node"):
        backend.set_hw_rules({"nodes": [{"node": "node001", "disabled": True}]})


def test_callers_at_the_same_moment_share_one_reload(rig):
    """
    Thirty callers at once are served by a handful of reloads, never one each, and none is
    answered by a reload that started before it asked.
    """
    import threading
    import time

    from trixd.plugins.prometheus.backend import SharedReload

    shared = SharedReload()
    started = []

    def slow_reload():
        """
        A reload that takes a moment, as systemctl does.
        """
        started.append(time.monotonic())
        time.sleep(0.2)
        return True, ""

    answers = []
    asked = {}

    def caller(number):
        """
        One writer asking for a reload.
        """
        asked[number] = time.monotonic()
        answers.append(shared.run(slow_reload))

    threads = [threading.Thread(target=caller, args=(n,)) for n in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(answers) == 30 and all(ok for ok, _ in answers)
    assert shared.runs <= 3, f"{shared.runs} reloads for 30 callers"
    assert started[-1] >= max(asked.values()) - 0.2 or shared.runs >= 2


def test_the_hardware_rules_of_every_node_can_be_listed(rig, tmp_path, monkeypatch):
    """
    Luna's export as a route: all nodes, some by name, counts only, and a missing one named.
    """
    from types import SimpleNamespace

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    every = backend.list_hw_rules(SimpleNamespace(query_params={}))["nodes"]
    assert every[0]["node"] == "node001.cluster" and every[0]["exists"] is True
    assert every[0]["rules"] == 1 and "document" in every[0]
    some = backend.list_hw_rules(SimpleNamespace(
        query_params={"nodes": "node001,node999", "detail": "no"}))["nodes"]
    assert some[0]["exists"] is True and "document" not in some[0]
    assert some[1] == {"node": "node999", "exists": False}


def test_a_renamed_node_carries_its_rule_files_and_needs_the_old_name(rig, tmp_path, monkeypatch):
    """
    Luna has already renamed the node, so only the caller still knows what the files were
    written under. Without it there is nothing to find them by, and a file left behind
    stops matching anything without ever being noticed.
    """
    from trixd.common.errors import BadRequest

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    before = sorted(os.listdir(backend._rules_dir()))
    assert any("node001" in name for name in before)

    with pytest.raises(BadRequest, match="was names what the node was called"):
        backend.rename_hw_rules("node001", {})
    with pytest.raises(BadRequest):
        backend.rename_hw_rules("node001", {"was": "   "})
    assert sorted(os.listdir(backend._rules_dir())) == before, "a refusal moved nothing"


def test_a_node_that_is_gone_has_its_rule_files_retired_not_deleted(rig, tmp_path, monkeypatch):
    """
    Retired means out of the glob Prometheus loads, under backup/, and still readable: a
    file retired by mistake can be put back. Deleting it could not be undone.
    """
    backend, reloads = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    rules = backend._rules_dir()
    before = len(reloads)
    live = [name for name in os.listdir(rules) if name.endswith(".rules")]
    assert live, "nothing was written, so retiring it proves nothing"

    answer = backend.remove_hw_rules("node001")
    assert answer["retired"], "nothing was reported as retired"
    assert [name for name in os.listdir(rules) if name.endswith(".rules")] == []
    kept = os.listdir(os.path.join(rules, "backup"))
    assert kept, "retired, but not kept where it can be put back"
    assert len(reloads) > before, "Prometheus was not told the rules changed"


def _factory_file(tmp_path, body=None):
    """
    The baseline the installer renders, as a file on disk.
    """
    import yaml
    path = tmp_path / "exporters.yaml"
    path.write_text(yaml.safe_dump(body if body is not None else {
        "nodes": {"node": {"port": 14200}, "lshw": {"port": 14208, "interval": "5m"}},
        "controllers": {"node": {"port": 14200}, "ha": {"port": 14205}},
    }), encoding="utf-8")
    return str(path)


def test_an_empty_table_renders_what_the_factory_file_says(rig, tmp_path):
    """
    The upgrade case, and the reason the baseline is a file rather than rows: a cluster that has
    changed nothing has no rows at all, and must render exactly what it rendered yesterday.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))

    resolved = exporters.for_node("node001", "compute", baseline, {}, {})
    assert sorted(resolved) == ["lshw", "node"]
    assert resolved["lshw"] == {"port": 14208, "interval": "5m"}


def test_a_group_overrides_the_cluster_and_a_node_overrides_its_group(rig, tmp_path):
    """
    The ladder, in one go: least specific first, each one having the last word over the one
    before it.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))
    rows = {"cluster": {"node": {"interval": "30s"}},
            "group:gpu": {"node": {"interval": "15s"}},
            "node:node002": {"node": {"interval": "5s"}}}

    assert exporters.for_node("node001", "compute", baseline, {}, rows)["node"]["interval"] == "30s"
    assert exporters.for_node("node001", "gpu", baseline, {}, rows)["node"]["interval"] == "15s"
    assert exporters.for_node("node002", "gpu", baseline, {}, rows)["node"]["interval"] == "5s"


def test_a_scope_switches_one_off_and_declares_one_the_factory_never_heard_of(rig, tmp_path):
    """
    A deviation is not only on or off: it carries a port, so a site can add its own exporter, and
    absence has to mean inherit or nothing could ever be turned off.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))
    rows = {"group:gpu": {"lshw": False, "storcli": {"port": 14211}}}

    resolved = exporters.for_node("node001", "gpu", baseline, {}, rows)

    assert "lshw" not in resolved, "a scope could not switch a factory exporter off"
    assert resolved["storcli"] == {"port": 14211}, "a scope could not declare its own exporter"
    assert "lshw" in exporters.for_node("node001", "compute", baseline, {}, rows), \
        "switching it off for one group took it from another"


def test_a_definition_moves_a_port_everywhere_without_touching_the_file(rig, tmp_path):
    """
    A site that runs an exporter on another port says so once, rather than per group.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))

    resolved = exporters.for_node("node001", "compute", baseline, {"node": {"port": 24200}}, {})

    assert resolved["node"]["port"] == 24200


def test_a_controller_resolves_on_its_own_ladder_and_not_the_nodes(rig, tmp_path):
    """
    The two ladders are separate on purpose: a rule written for every node must not land on a
    controller, which is not one of Luna's nodes.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))
    rows = {"cluster": {"lshw": False},
            "controllers": {"ha": {"interval": "1m"}},
            "ctrl2.cluster": {"ha": False}}

    one = exporters.for_controller("ctrl1.cluster", baseline, {}, rows)
    two = exporters.for_controller("ctrl2.cluster", baseline, {}, rows)

    assert one["ha"]["interval"] == "1m", "the controllers scope did not apply"
    assert "ha" not in two, "a controller's own row did not override the controllers scope"
    assert "node" in one and "node" in two
    assert "lshw" not in baseline["controllers"], "the node baseline leaked into the controllers"


def test_the_factory_file_is_read_again_only_when_it_moves(rig, tmp_path, monkeypatch):
    """
    A render asks for the baseline on every pass, and a pass can follow every node change. The
    file is parsed when it changes and not once per boot in a boot storm.
    """
    import yaml
    from trixd.plugins.prometheus import exporters

    path = _factory_file(tmp_path)
    reads = []
    original = yaml.safe_load
    monkeypatch.setattr(yaml, "safe_load", lambda *a, **k: reads.append(1) or original(*a, **k))

    exporters.factory(path)
    exporters.factory(path)
    exporters.factory(path)
    assert len(reads) == 1, f"the file was parsed {len(reads)} times for three asks"

    os.utime(path, (0, 0))
    exporters.factory(path)
    assert len(reads) == 2, "a changed file was not read again"


def test_a_broken_factory_file_keeps_what_was_loaded(rig, tmp_path):
    """
    A half-written or invalid file must not empty Prometheus's target list. It is said loudly and
    the daemon renders what it already had.
    """
    from trixd.plugins.prometheus import exporters

    path = _factory_file(tmp_path)
    good = exporters.factory(path)
    assert good.get("nodes")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("nodes: {node: {port: 14200}\n")      # never closed
    os.utime(path, (1, 1))

    assert exporters.factory(path).get("nodes"), "a broken file emptied the baseline"


def test_the_render_scrapes_what_the_factory_file_says_and_the_controllers_it_names(
        rig, tmp_path, monkeypatch):
    """
    The whole point of the file: the ten the product installs rather than the five that were
    typed into the daemon, and the controllers as targets in their own right.

    The labels are asserted, not just the count, because dashboards and alert rules group by
    them - they are what a customer sees, and they must match what the file_sd this replaces
    emitted: exporter, luna_group, hostname.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    path = _factory_file(tmp_path, {
        "nodes": {"node": {"port": 14200}, "amd": {"port": 14209}},
        "controllers": {"node": {"port": 14200}, "ha": {"port": 14205}},
        "hosts": {"ctrl1.cluster": {"address": "10.141.255.252"}},
    })
    backend.config({"exporters_file": path})

    rendered = backend.render_targets()
    by_target = {entry["targets"][0]: entry["labels"] for entry in rendered}

    assert "10.141.255.252:14205" in by_target, "the controller is not scraped for ha"
    assert by_target["10.141.255.252:14205"] == {
        "exporter": "ha", "luna_group": "controller", "hostname": "ctrl1.cluster"}, \
        "a controller's labels no longer match what file_sd emitted"
    assert by_target["node001.cluster:14209"]["exporter"] == "amd", \
        "an exporter the product installs but the daemon never listed is still not scraped"
    assert by_target["node001.cluster:14209"]["luna_group"] == "compute"


def test_a_deviation_changes_what_one_group_is_scraped_on(rig, tmp_path, monkeypatch):
    """
    End to end through the render: a site turns one exporter off for a group and moves another's
    port, and only that group moves.
    """
    from trixd.utils import declarations

    backend = _prometheus_rig(tmp_path, monkeypatch, {
        "node001": {"hostname": "node001.cluster", "group": "compute"},
        "node002": {"hostname": "node002.cluster", "group": "gpu"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {
        "nodes": {"node": {"port": 14200}, "lshw": {"port": 14208}}})})
    declarations.write("prometheus", "exporter-scope", "group:gpu",
                       {"lshw": False, "node": {"port": 24200}})

    by_host = {}
    for entry in backend.render_targets():
        by_host.setdefault(entry["labels"]["hostname"], []).append(entry["targets"][0])

    assert sorted(by_host["node001.cluster"]) == ["node001.cluster:14200",
                                                  "node001.cluster:14208"]
    assert by_host["node002.cluster"] == ["node002.cluster:24200"], \
        "the gpu group did not get its own port, or kept the exporter it switched off"


def test_an_over_long_scope_is_refused_rather_than_failing_in_the_database(rig, tmp_path,
                                                                           monkeypatch):
    """
    A path segment reaches the declaration's name column, which holds 255 characters. Without
    this the caller's mistake arrives as a 500 from the database rather than as a refusal.
    """
    import pytest
    from trixd.common.errors import BadRequest

    backend = _prometheus_rig(tmp_path, monkeypatch, {})

    with pytest.raises(BadRequest):
        backend.set_applied("group:" + "x" * 300, {"exporters": {"node": True}})


def _bound(endpoint_path, verb, handler_name, tmp_path, monkeypatch, nodes=None):
    """
    One of this plugin's endpoints as the daemon binds it: the manifest's own schema turned
    into the pydantic model FastAPI would use, in front of the real handler.

    Calling the backend directly - as every other test here does - skips exactly the layer
    where a schema and a handler can disagree while the suite stays green.
    """
    import yaml
    from trixd.core.plugins import binder, manifest as manifests

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    document = yaml.safe_load(open(os.path.join(here, "trixd/plugins/prometheus/manifest.yaml"),
                                   encoding="utf-8"))
    parsed = manifests.parse(document)
    endpoint = [e for e in parsed.endpoints
                if e.path == endpoint_path and verb in [m.upper() for m in e.methods]][0]
    models = manifests.build_models(parsed)
    backend = _prometheus_rig(tmp_path, monkeypatch, nodes or {})
    model = models.get(endpoint.body_schema) if endpoint.body_schema else None
    operation = binder._endpoint_function(getattr(backend, handler_name), endpoint, model,
                                          "prometheus", verb)
    import inspect
    operation.body_model = inspect.signature(operation).parameters["body"].annotation
    return operation, [e for e in document["endpoints"]
                       if e["path"] == endpoint_path and verb in e["methods"]][0]


def test_the_scope_endpoint_takes_the_body_its_own_manifest_documents(rig, tmp_path,
                                                                      monkeypatch):
    """
    The documented request example, through the model the manifest declares. A schema asking
    for one shape while the handler reads another answers 422 to the only body anybody would
    send, and no test that calls the backend directly can see it.
    """
    operation, documented = _bound("exporters/applied/<where>", "PATCH", "set_applied",
                                   tmp_path, monkeypatch)

    answer = operation(where="group:gpu",
                       body=operation.body_model(**documented["request_example"]))

    assert answer["exporters"]["lshw"] is False, answer


def test_changing_one_field_through_the_model_keeps_the_rest(rig, tmp_path, monkeypatch):
    """
    The model fills every unset optional with None. Written straight into the document those
    blank the port, and the exporter then resolves to nothing on every node - from a PATCH that
    only meant to change an interval.
    """
    operation, _ = _bound("exporters/<exporter>", "PATCH", "set_exporter", tmp_path, monkeypatch)
    model = operation.body_model

    operation(exporter="node", body=model(port=14200))
    answer = operation(exporter="node", body=model(interval="15s"))

    assert answer["fields"] == {"port": 14200, "interval": "15s"}, \
        "an interval-only change dropped the port"


def test_a_write_wakes_the_render_that_reads_it(rig, monkeypatch):
    """
    A render's bare manifest input is compared plugin-qualified, so a bare kind wakes nothing
    and the change waits for an unrelated node to move. The symptom is a deviation that applies
    on the controller nobody used.
    """
    from trixd.core.render import engine
    from trixd.plugins.prometheus import backend as prometheus

    woken = []
    monkeypatch.setattr(engine, "current",
                        lambda: type("E", (), {"notify": lambda self, changed:
                                               woken.append(changed) or []})())
    monkeypatch.setattr(prometheus, "renders", engine.current)

    prometheus.Backend().set_applied("cluster", {"exporters": {"node": True}})

    assert woken, "nothing was notified at all"
    # Against the render's own matcher and this plugin's declared inputs, so renaming an input
    # breaks the test rather than leaving it green while the render stops waking.
    import yaml
    from trixd.core.plugins import manifest as manifests
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parsed = manifests.parse(yaml.safe_load(
        open(os.path.join(here, "trixd/plugins/prometheus/manifest.yaml"), encoding="utf-8")))
    targets = [e for e in parsed.endpoints if e.path == "renders/targets"][0]
    render = engine.Render("prometheus", "renders/targets", targets.render, lambda: [])
    assert all(render.wants(key) for key in woken), \
        f"notified {woken}, which the targets render does not match"


def test_a_site_cannot_rename_the_labels_everything_groups_by(rig, tmp_path, monkeypatch):
    """
    Dashboards group by exporter, luna_group and hostname - 122 queries on luna_group alone -
    so a label carried from the file must not be able to take one of those names.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {"nodes": {
        "node": {"port": 14200,
                 "labels": {"exporter": "node_v2", "hostname": "elsewhere", "rack": "7"}}}})})

    labels = backend.render_targets()[0]["labels"]

    assert labels["exporter"] == "node" and labels["hostname"] == "node001.cluster"
    assert labels["rack"] == "7", "a label of the site's own was dropped"


def test_a_label_prometheus_cannot_parse_does_not_take_the_document_down(rig, tmp_path,
                                                                        monkeypatch):
    """
    http_sd refuses the whole response over one invalid label, so one site's typo would stop
    every target being discovered rather than its own. A name it will not accept is dropped; a
    bare integer is what an operator plainly meant and is carried as its string.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {"nodes": {
        "node": {"port": 14200, "labels": {"gpu-type": "a100", "rack": 7}}}})})

    labels = backend.render_targets()[0]["labels"]

    assert "gpu-type" not in labels, "a label name Prometheus refuses was emitted"
    assert labels["rack"] == "7", "a bare integer label was thrown away rather than carried"
    assert labels["exporter"] == "node"


def test_a_port_where_a_mapping_belongs_does_not_stop_discovery(rig, tmp_path, monkeypatch):
    """
    An ordinary typo in the file must not reach the render: an exception there leaves a
    restarted daemon with no document at all, which answers 404 to every poll.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {"nodes": {"node": 14200}})})

    rendered = backend.render_targets()

    assert rendered, "a typo emptied the service discovery document"
    assert {entry["labels"]["exporter"] for entry in rendered} == {"node", "ipmi", "nvidia",
                                                                   "slurm_job", "lshw"}, \
        "the file was refused but the shipped baseline did not take over"


def test_an_interval_reaches_prometheus_or_it_is_not_a_feature(rig, tmp_path, monkeypatch):
    """
    http_sd carries a per-target interval only as __scrape_interval__. Stored, echoed back and
    never emitted, the field would be a setting that reads as applied and changes nothing.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {"nodes": {
        "lshw": {"port": 14208, "interval": "5m", "timeout": "30s"}}})})

    labels = backend.render_targets()[0]["labels"]

    assert labels["__scrape_interval__"] == "5m", "the interval never reached the target"
    assert labels["__scrape_timeout__"] == "30s"


def test_the_listing_says_when_it_is_not_reading_the_file(rig, tmp_path, monkeypatch):
    """
    Comparing two controllers is the endpoint's purpose. Reporting the exporters built into the
    daemon under the file's own path and digest tells the operator the files differ in content
    when the truth is that one controller is not reading its file at all.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    backend.config({"exporters_file": str(tmp_path / "there-is-no-such-file.yaml")})

    factory = backend.list_exporters()["factory"]

    assert factory["read"] is False
    assert factory["reason"], "no reason was given for not using the file"
    assert backend.list_exporters()["factory"]["nodes"], "the fallback is not reported at all"


def test_the_api_refuses_a_shape_the_render_cannot_use(rig, tmp_path, monkeypatch):
    """
    The file's reader checks what it reads; the API is the other way in. A labels value that is
    not a mapping reaches the render otherwise, and after a restart there is no document at all
    - which is 404 to every poll, on both controllers, from one authenticated call.
    """
    import pytest
    from trixd.common.errors import BadRequest

    backend = _prometheus_rig(tmp_path, monkeypatch, {})

    with pytest.raises(BadRequest):
        backend.set_applied("cluster", {"exporters": {"node": {"labels": "rack=7"}}})
    with pytest.raises(BadRequest):
        backend.set_exporter("node", {"labels": "rack=7"})


def test_a_path_word_cannot_be_taken_as_an_exporter_name(rig, tmp_path, monkeypatch):
    """
    PATCH /exporters/applied with the scope left off matches the one-segment route, so without
    this it defines an exporter named after the path word and scrapes it on every node.
    """
    import pytest
    from trixd.common.errors import BadRequest

    backend = _prometheus_rig(tmp_path, monkeypatch, {})

    with pytest.raises(BadRequest):
        backend.set_exporter("applied", {"port": 14200})


def test_a_bad_entry_in_one_section_does_not_cost_the_others(rig, tmp_path, monkeypatch):
    """
    Refusing the whole file for one typo takes away the sections that were right: a mistake in
    hosts would cost every node its exporters, which is a far bigger loss than the mistake.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {
        "nodes": {"node": {"port": 14200}, "amd": {"port": 14209}},
        "hosts": {"ctrl1.cluster": "10.141.255.252"}})})      # a string where a mapping belongs

    rendered = backend.render_targets()

    assert {e["labels"]["exporter"] for e in rendered} == {"node", "amd"}, \
        "a typo in hosts took the node section down with it"
    assert not [e for e in rendered if e["labels"].get("luna_group") == "controller"], \
        "the refused host was scraped anyway"


def test_a_timeout_sent_to_the_route_is_not_quietly_dropped(rig, tmp_path, monkeypatch):
    """
    A field the handler emits but the schema does not declare is dropped by the model before
    the handler ever sees it, and the call still answers 200 - a setting that reads as applied
    and does nothing, which is the whole defect this field was added to fix.
    """
    operation, _ = _bound("exporters/<exporter>", "PATCH", "set_exporter", tmp_path, monkeypatch)
    model = operation.body_model

    answer = operation(exporter="lshw", body=model(port=14208, timeout="10s"))

    assert answer["fields"].get("timeout") == "10s", \
        f"the timeout never reached the handler: {answer['fields']}"


def test_a_duration_that_is_not_one_is_refused_on_both_routes(rig, tmp_path, monkeypatch):
    """
    A bare number is not a duration: Prometheus's own parser rejects __scrape_interval__ and
    drops the target, silently from here. The scope route takes a free mapping, so without a
    check of its own it would accept what the typed route refuses - one field, two answers.
    """
    import pytest
    from trixd.common.errors import BadRequest

    backend = _prometheus_rig(tmp_path, monkeypatch, {})

    with pytest.raises(BadRequest):
        backend.set_applied("group:gpu", {"exporters": {"node": {"interval": 30}}})
    with pytest.raises(BadRequest):
        backend.set_exporter("node", {"port": "14200 "})


def test_no_verb_takes_a_path_word_as_an_exporter_name(rig, tmp_path, monkeypatch):
    """
    Derived from the manifest rather than from a list someone keeps by hand: every literal word
    that appears as a path segment under exporters/ must be refused as an exporter name, on
    every verb bound to the one-segment route. A new route adds a new word for free.
    """
    import pytest
    import yaml
    from trixd.common.errors import BadRequest
    from trixd.plugins.prometheus import backend as prometheus

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    document = yaml.safe_load(open(os.path.join(here, "trixd/plugins/prometheus/manifest.yaml"),
                                   encoding="utf-8"))
    words = set()
    for endpoint in document["endpoints"]:
        parts = endpoint["path"].split("/")
        if parts[0] == "exporters":
            words.update(p for p in parts[1:] if not p.startswith("<"))
    assert words, "no path words were found under exporters/, so this test proves nothing"
    assert words <= set(prometheus.RESERVED), \
        f"{words - set(prometheus.RESERVED)} can be taken as an exporter name"

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    for word in words:
        with pytest.raises(BadRequest):
            backend.set_exporter(word, {"port": 14200})
        with pytest.raises(BadRequest):
            backend.remove_exporter(word)


def test_a_section_written_empty_means_nothing_here(rig, tmp_path, monkeypatch):
    """
    The delicate half of the rule: a section emptied by refusals falls back to what the daemon
    was built with, while one an operator deliberately wrote empty means none - and only this
    pins the second, so loosening the condition would silently start scraping five ports.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {
        "nodes": {}, "controllers": {"ha": {"port": 14205}},
        "hosts": {"ctrl1.cluster": {"address": "10.0.0.1"}}})})

    rendered = backend.render_targets()

    assert not [e for e in rendered if e["labels"].get("hostname") == "node001.cluster"], \
        "an empty nodes section fell back to the exporters built into the daemon"
    assert [e for e in rendered if e["labels"].get("luna_group") == "controller"], \
        "the controllers went with it"


def test_the_listing_says_what_the_file_lost(rig, tmp_path, monkeypatch):
    """
    The file is forgiving on purpose, so the one place an operator can see what it forgave is
    the API - a log line from start-up has rotated long before anyone diffs the two.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    backend.config({"exporters_file": _factory_file(tmp_path, {"nodes": {
        "node": {"port": 14200, "labels": {"gpu-type": "a100"}},
        "lshw": {"port": 14208, "interval": 30}}})})

    listed = backend.list_exporters()["factory"]

    assert listed["read"] is True
    assert any("gpu-type" in line for line in listed["ignored"]), listed["ignored"]
    assert any("interval" in line for line in listed["ignored"]), listed["ignored"]


def test_a_hardware_string_cannot_break_the_selector(rig, monkeypatch):
    """
    Hardware strings come off the node and carry whatever the vendor wrote: an inch mark in
    a disk's product string is ordinary. Interpolated raw it closes the quote early, and the
    real promtool then refuses the whole file - "unexpected identifier ... in label
    matching" - which aborts the reload and leaves Prometheus serving its previous
    configuration, so every later rule change is silently not applied.

    Asserted over every label at once, so a value put into a selector without the escape is
    caught here rather than on a cluster. It has to be caught here: promtool is not
    installed everywhere, so the check at write time cannot be what closes this.
    """
    from trixd.plugins.prometheus.backend import (COMPONENT_LABELS, MATCHER, Backend,
                                                  quoted)

    awkward = '2.5" SATA\\ SSD'
    component = {label: awkward for label in COMPONENT_LABELS}
    backend = Backend()
    monkeypatch.setattr(backend, "_components", lambda host: [component])
    monkeypatch.setattr(backend, "settings", lambda: {})

    expr = backend._generate_hw_rules("node001")["groups"][0]["rules"][0]["expr"]
    carried = {key: value for key, _, value in MATCHER.findall(expr)}

    # The values, not the number of them: the matcher pattern re-synchronises after a value
    # that closes its quote early, so an unescaped selector still parses as the right COUNT
    # of matchers - each one truncated. Counting them passes either way.
    assert set(carried) == set(COMPONENT_LABELS), carried
    for key, value in carried.items():
        assert value == quoted(awkward), f"{key} carries {value!r}, not the whole string"


def test_writing_a_rule_file_forks_nothing(rig, monkeypatch, tmp_path):
    """
    The check at write time is in process, and has to stay that way. A fork is about 35 ms
    before promtool reads anything, and this runs once per node: at a thousand nodes that
    is 35 s of forks spread over the request threads, and the same again inside the
    reconcile pass that is supposed to return to idle. Measured in process instead: 0.2 ms
    per node for a twenty-component file.

    What promtool adds is the shape of our own rules, which is the same for every node, so
    it is paid once per reload over the files that changed - see _reload_now.
    """
    from trixd.plugins.prometheus import backend as prometheus

    forked = []
    monkeypatch.setattr(prometheus.shell, "run",
                        lambda *args, **kwargs: forked.append(args[0]))

    backend = prometheus.Backend()
    document = {"groups": [{"name": "trinityx_hw", "rules": [
        {"alert": "A", "expr": 'absent(lshw_device{hostname="node001"})'}]}]}
    backend._write_rules(str(tmp_path / "trix.hw.node001.rules"), document)

    assert forked == [], f"writing one node's rule file forked {forked}"


def test_a_refused_reload_reaches_the_answer_where_nobody_is_waiting(rig, monkeypatch,
                                                                     tmp_path):
    """
    Three paths reloaded and dropped the outcome. The files are written either way, so a
    refusal must not turn a completed write into a failure - but dropping it left the one
    case that matters invisible: Prometheus refused the files, went on serving its previous
    configuration, and the answer said the rules were retired.
    """
    from trixd.plugins.prometheus import backend as prometheus

    backend = prometheus.Backend()
    monkeypatch.setattr(backend, "_rules_dir", lambda: str(tmp_path))
    monkeypatch.setattr(backend, "_reload_now", lambda: (False, "exit 1: refused"))
    # One file to retire, so the path that reloads is the one taken.
    written = os.path.join(str(tmp_path), prometheus.HW_RULES.format(host="node001"))
    with open(written, "w", encoding="utf-8") as handle:
        handle.write("---\ngroups: []\n")

    answer = backend.remove_hw_rules("node001")

    assert answer["retired"], "nothing was retired, so the reload path was not taken"
    reloaded = answer.get("reloaded")
    assert reloaded is not None, f"the reload outcome was dropped: {answer}"
    assert reloaded["ok"] is False, answer
    assert "refused" in reloaded["message"], answer


def test_a_renamed_nodes_old_file_does_not_overwrite_its_new_one(rig, monkeypatch, tmp_path):
    """
    After a rename a node has a file under the name it answers to now, and one under the
    name it had. The reconcile read the old one, relabelled it and wrote it to the new one -
    promoting what the node was called over whatever has been written since - and the stamp
    that write set then made the loop skip the real file.
    """
    from trixd.plugins.prometheus import backend as prometheus

    backend = prometheus.Backend()
    monkeypatch.setattr(backend, "_rules_dir", lambda: str(tmp_path))
    # The file under the node's short name is the one that maps to it and yet is not where
    # it belongs - a file whose name answers to no node is retired by another path entirely,
    # so naming the old file something unrelated would not reach this code at all.
    stale = os.path.join(str(tmp_path), prometheus.HW_RULES.format(host="node001"))
    fresh = os.path.join(str(tmp_path),
                         prometheus.HW_RULES.format(host="node001.example.com"))

    def document(marker):
        """One rule naming a host, so which file won is readable afterwards."""
        return {"groups": [{"name": prometheus.HW_GROUP, "rules": [{
            "alert": prometheus.HW_ALERT, "serial": marker,
            "expr": f'absent(lshw_device{{hostname="{marker}",luna_group="compute"}})'}]}]}

    files.write_yaml(stale, document("short-name"))
    files.write_yaml(fresh, document("node001.example.com"))

    outcome = backend._reconcile_hw_rules(
        {"node001": {"hostname": "node001.example.com", "group": "compute"}})

    # The precondition: the stale file was recognised as this node's and dealt with, rather
    # than retired as a file naming nobody - which is a different path and would prove
    # nothing about this one.
    assert outcome["superseded"] == [os.path.basename(stale)], outcome
    assert outcome["retired"] == [], outcome

    kept = _read_yaml_file(fresh)["groups"][0]["rules"][0]
    assert kept["serial"] == "node001.example.com", \
        f"the file the node answers to now carries {kept['serial']!r}, from the stale one"
    assert not os.path.isfile(stale), "the stale file is still there"


def test_two_nodes_sharing_a_short_name_do_not_collapse_onto_one_file(rig, monkeypatch,
                                                                     tmp_path):
    """
    Two nodes whose names differ only past the first dot share a short name. Taking the
    first of them relabelled one node's file to the other and discarded the loser's, so two
    genuinely different nodes ended up as one. Neither file is touched instead: a wrong
    answer here moves one node's rules onto another, which is worse than an old file.
    """
    from trixd.plugins.prometheus import backend as prometheus

    backend = prometheus.Backend()
    monkeypatch.setattr(backend, "_rules_dir", lambda: str(tmp_path))
    path = os.path.join(str(tmp_path), prometheus.HW_RULES.format(host="node001"))
    files.write_yaml(path, {"groups": [{"name": prometheus.HW_GROUP, "rules": [{
        "alert": prometheus.HW_ALERT,
        "expr": 'absent(lshw_device{hostname="node001",luna_group="first"})'}]}]})

    outcome = backend._reconcile_hw_rules({
        "node001.one.example": {"hostname": "node001.one.example", "group": "first"},
        "node001.two.example": {"hostname": "node001.two.example", "group": "second"},
    })

    assert os.path.isfile(path), "the file of a node that cannot be identified was discarded"
    assert outcome["repaired"] == [], outcome
    assert outcome["retired"] == [], outcome
    expr = _read_yaml_file(path)["groups"][0]["rules"][0]["expr"]
    assert 'luna_group="first"' in expr, f"the file was relabelled to a guess: {expr}"


def _read_yaml_file(path: str) -> dict:
    """
    One document off disk, for reading back what a reconcile left.
    """
    import yaml
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)
