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

import logging
import os
from types import SimpleNamespace

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


def test_a_systemctl_that_did_not_run_is_not_silent(rig, monkeypatch):
    """
    A whole-command failure reads as every service being unknown, and one unit systemd
    answered nothing about reads the same, so only the journal tells the two apart.
    """
    from trixd.plugins.services import backend as services

    monkeypatch.setattr(services.shell, "run",
                        lambda *a, **k: Ran("", 1, "Failed to get properties"))
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    level = services.LOGGER.level
    services.LOGGER.addHandler(handler)
    services.LOGGER.setLevel(logging.DEBUG)
    try:
        listed = services.Backend().list_services()
    finally:
        services.LOGGER.removeHandler(handler)
        services.LOGGER.setLevel(level)

    assert listed["services"], "the catalogue answered nothing, so nothing was read"
    assert all(state["active"] == "unknown" for state in listed["services"].values()), \
        "the stand-in answered a state after all"
    said = [record.getMessage() for record in records if record.levelno >= logging.ERROR]
    assert said, "a systemctl that did not run left no trace at all"
    assert "Failed to get properties" in said[0], said


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
    assert memory["alert"] == "MissingHardwareComponent"
    assert memory.get("for") == prometheus.DEFAULT_CONFIG["hw_alert_grace"]
    assert memory["expr"] == ('absent(lshw_device{hostname="node001.cluster",luna_group="compute",'
                              'class="memory",description="DIMM",product="P",vendor="V",'
                              'serial="S1"})')
    assert "serial=" not in infiniband["expr"]
    assert (tmp_path / "trix.hw.node001.cluster.rules").is_file()


def test_a_generated_hardware_alert_waits_before_it_fires(rig, monkeypatch):
    """
    A generated hardware alert carries a grace period, and its length is the configured one
    rather than a constant.

    absent() is true while a node's rule file is stale, which it briefly is after the node
    changes group, and the drainer acts on the nhc label as soon as an alert fires - so an
    alert with nothing to wait out drains a node that was never away.
    """
    from trixd.plugins.prometheus.backend import COMPONENT_LABELS, DEFAULT_CONFIG, Backend

    backend = Backend()
    monkeypatch.setattr(backend, "_components",
                        lambda host: [{label: "x" for label in COMPONENT_LABELS}])
    monkeypatch.setattr(backend, "settings", lambda: {})

    shipped = backend._generate_hw_rules("node001")["groups"][0]["rules"][0]
    assert shipped.get("for"), \
        "a generated hardware alert has no for clause, so it fires on the first evaluation"

    backend.config(body={"hw_alert_grace": "11m"})
    configured = backend._generate_hw_rules("node001")["groups"][0]["rules"][0]
    assert configured.get("for") == "11m", \
        f"the grace period is not read from the configuration: {configured.get('for')!r}"
    assert shipped["for"] == DEFAULT_CONFIG["hw_alert_grace"]


def test_rules_sent_in_keep_the_grace_period_they_were_sent_with(rig, tmp_path, monkeypatch):
    """
    The grace period on a generated hardware rule is this plugin's; the one on a rule written
    in the alertx wizard is the user's, and it survives being stored and written.

    The two paths share only the writer, which takes the document as it is given. A default
    applied there instead would replace a length the user chose, and nothing would say so.
    """
    import yaml

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    sent = {"groups": [
        {"name": "trinityx_generic",
         "rules": [{"alert": "NodeDown", "expr": 'up{exporter="node"} == 0', "for": "90s",
                    "labels": {"severity": "critical"},
                    "annotations": {"description": "it has not answered"}}]},
        {"name": "service_slurm",
         "rules": [{"alert": "SlurmDown", "expr": 'up{exporter="slurm"} == 0',
                    "labels": {"severity": "warning"}}]}]}

    backend.rules(body=sent)

    assert backend.rules()["groups"][0]["rules"][0].get("for") == "90s"
    written = yaml.safe_load((tmp_path / "trix.generic.rules").read_text(encoding="utf-8"))
    assert written["groups"][0]["rules"][0].get("for") == "90s", written
    # And one sent without a grace period is left without one, rather than taking a default
    # meant for the rules this plugin generates itself.
    service = yaml.safe_load((tmp_path / "trix.service.rules").read_text(encoding="utf-8"))
    assert "for" not in service["groups"][0]["rules"][0], service


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


# Which of Luna's tables each field of the config/node document is assembled from. The
# document is Luna's, so a producer reading a field depends on every table beneath it -
# `hostname` is the node's name and the name of the network its provisioning interface
# has an address on, which is four tables for one string.
NODE_DOCUMENT: dict = {
    "hostname": ("node", "nodeinterface", "ipaddress", "network"),
    "group": ("group",),
    "provision_interface": ("node", "group"),
    "interfaces": ("nodeinterface", "ipaddress"),
    "interface": ("nodeinterface",),
    "ipaddress": ("ipaddress",),
    "ipaddress_ipv6": ("ipaddress",),
    "macaddress": ("nodeinterface",),
    "network": ("network",),
}


def _watch(value, seen: set):
    """
    The same value, with every record inside it watching what is read of it.
    """
    if isinstance(value, dict):
        return _Watched(value, seen)
    if isinstance(value, list):
        return [_watch(one, seen) for one in value]
    return value


class _Watched(dict):
    """
    A record as Luna answered it, remembering which of its fields a producer read.
    """

    def __init__(self, record: dict, seen: set) -> None:
        """
        Hold the record, and the set every read of it is noted in.
        """
        super().__init__({key: _watch(value, seen) for key, value in record.items()})
        self.seen = seen

    def __getitem__(self, key):
        """
        One field, noted.
        """
        self.seen.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        """
        One field or a default, noted: a field read and found empty is still read.
        """
        self.seen.add(key)
        return super().get(key, default)

    def __contains__(self, key) -> bool:
        """
        Whether a field is here, noted: absence is read as much as presence is.
        """
        self.seen.add(key)
        return super().__contains__(key)

    def keys(self):
        """
        Every field, so a producer that takes the whole record depends on all of them.
        """
        self.seen.update(super().keys())
        return super().keys()

    def items(self):
        """
        As keys: the whole record was read.
        """
        self.seen.update(super().keys())
        return super().items()

    def values(self):
        """
        As keys: the whole record was read.
        """
        self.seen.update(super().keys())
        return super().values()


def _declares_every_table_beneath(key: str, declared: dict, seen: set) -> None:
    """
    Every Luna table under a field this producer read is one the render declares.
    """
    assert key in declared, f"no shipped manifest declares a render at {key}"
    assert seen, f"{key} read no field of Luna's node document, so this proves nothing"
    for field in sorted(seen):
        assert field in NODE_DOCUMENT, \
            (f"{key} reads {field} of Luna's node document and this test cannot say which "
             f"of Luna's tables carry it; add it to NODE_DOCUMENT and declare those tables")
        for table in NODE_DOCUMENT[field]:
            assert f"luna.{table}" in declared[key], \
                (f"{key} reads {field}, which Luna builds from {table}, and does not declare "
                 f"luna.{table} - so nothing wakes it when that table alone moves")


def test_every_render_declares_the_luna_tables_its_producer_reads(rig, tmp_path, monkeypatch):
    """
    What a render declares is kept by hand beside what its producer reads, and the two had
    already drifted twice: both prometheus renders label by the group name without declaring
    the group table, and the address the targets document publishes comes out of two tables
    no render named - so a group rename and a re-address left the published documents stale
    with nothing to say so.

    Driven from the shipped manifests, against a Luna whose records note what was read of
    them, so a field read without its tables declared fails here. What it cannot see is a
    field read from anything but the node document, and a table whose contribution to a
    field changes at Luna; NODE_DOCUMENT is where that is written down.
    """
    import glob as globbing

    import yaml

    from trixd.plugins.genders import backend as genders
    from trixd.plugins.slurm import backend as slurm

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    declared = {}
    for found in sorted(globbing.glob(os.path.join(here, "trixd/plugins/*/manifest.yaml"))):
        with open(found, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        for endpoint in document.get("endpoints") or []:
            if endpoint.get("render"):
                declared[f"{document['plugin']}/{endpoint['path']}"] = \
                    set(endpoint["render"].get("inputs") or [])
    assert len(declared) >= 4, f"the manifests declared no renders to check: {declared}"

    record = {"hostname": "node001.cluster", "group": "compute",
              "provision_interface": "BOOTIF",
              "interfaces": [{"interface": "BOOTIF", "ipaddress": "10.141.0.1",
                              "network": "cluster", "macaddress": "aa:bb:cc:dd:ee:01"}]}

    held = {}
    backend = _prometheus_rig(tmp_path, monkeypatch, held)
    _hw_file(tmp_path, "node001.cluster", "compute")
    for key, produce in (("prometheus/renders/targets", backend.render_targets),
                         ("prometheus/renders/rule-files", backend.render_rule_files)):
        seen = set()
        held.clear()
        held["node001"] = _Watched(record, seen)
        produce()
        _declares_every_table_beneath(key, declared, seen)

    # The genders render reads the slurm plugin's node_set by its canonical path, so one
    # stand-in covers both; a genders plugin that grows its own reader fails here first.
    assert genders.node_set is slurm.node_set, \
        "genders no longer reads the slurm plugin's node_set, so it needs its own run here"
    seen = set()
    monkeypatch.setattr(slurm, "luna",
                        lambda: SimpleNamespace(nodes=lambda: {"node001": _Watched(record,
                                                                                   seen)}))
    slurm.node_set()
    for key in sorted(declared):
        if key.startswith(("slurm/", "genders/")):
            _declares_every_table_beneath(key, declared, seen)


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


def test_a_batch_is_not_recorded_as_applied_for_what_it_did_not_apply(rig, tmp_path,
                                                                     monkeypatch):
    """
    A replicating handler says what it did not do in the reserved `skipped` string, so the
    sender holds the row rather than recording the lot as applied. Both hw-rules write
    paths are asserted, and the two kinds of failure stay apart in what an operator reads.
    """
    from trixd.core.replication import applied, outbox
    from trixd.plugins.prometheus import backend as prometheus

    known = {"node001": {"hostname": "node001.cluster", "group": "compute"},
             "node002": {"hostname": "node002.cluster", "group": "compute"}}
    backend = _prometheus_rig(tmp_path, monkeypatch, known)

    def query(url, params=None, **_):
        """
        Prometheus, which has scraped node001 and has nothing for node002 yet.
        """
        host = params["query"].split('hostname="')[1].split('"')[0]
        result = [] if host.startswith("node002") else [
            {"metric": {"hostname": host, "luna_group": "compute", "class": "memory",
                        "description": "DIMM", "product": "P", "vendor": "V",
                        "serial": "S"}}]
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"result": result}})

    monkeypatch.setattr(prometheus.requests, "get", query)
    asked = {"nodes": [{"node": "node001", "nhc": False},
                       {"node": "node002", "force": True},
                       {"node": "node999"}]}
    answer = backend.set_hw_rules(asked)

    by_node = {item["node"]: item for item in answer["nodes"]}
    assert by_node["node001"]["ok"], answer
    assert not by_node["node002"]["ok"] and not by_node["node999"]["ok"], answer
    turned_down = applied.declined(answer)
    assert turned_down != "", \
        f"a batch that applied only part of itself reads as applied: {answer}"
    assert turned_down.startswith("2 of 3"), \
        f"the decline does not say how much of the batch did not land: {turned_down}"
    # Both reasons reach whoever reads the refusal: a node this peer's Luna does not hold
    # and a host it has not scraped want different attention.
    assert "not a node Luna knows" in turned_down, turned_down
    assert "has not been scraped yet" in turned_down, turned_down
    assert applied.declined(backend.repair_hw_rules("node999")) != "", \
        "the single-node path stopped saying it declined"

    # A decline is deliberately not remembered, so the sweep's next offer runs the handler
    # again: what did apply has to read as unchanged rather than being written a second time.
    again = {item["node"]: item for item in backend.set_hw_rules(asked)["nodes"]}
    assert again["node001"]["changed"] is False, \
        f"the node that did apply was written again on the next offer: {again}"

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    monkeypatch.setattr(outbox, "trust", lambda: False)
    monkeypatch.setattr(outbox, "peer_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(outbox, "held_token", lambda peer: "token")
    monkeypatch.setattr(outbox.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, text="",
        json=lambda: {"applied": "prometheus hw-rules", "origin": "tester",
                      "result": answer}))

    outbox.submit("ctrl2", "prometheus", "hw-rules", "PATCH", {}, asked, "req-batch",
                  "tester")
    outbox._failures.clear()
    outbox._open.clear()
    try:
        result = outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()

    assert result["sent"] == 0, f"the partly applied batch was counted as sent: {result}"
    still = outbox.owed()["actions"]
    assert [one["path"] for one in still] == ["hw-rules"], \
        f"nothing is owed, so the batch was recorded as applied: {still}"
    assert "2 of 3" in still[0]["message"], still[0]["message"]


def test_a_decline_is_built_to_the_characters_the_sender_keeps(rig, tmp_path, monkeypatch):
    """
    A batch declining per node is bounded, and both causes still reach whoever reads it.

    One of the two reasons names the node it is about, so a set of them deduplicates
    nothing: a cluster that Prometheus has not scraped yet declines every node with its own
    sentence. Built whole and cut by the sender, the characters go on node names and the
    other cause - a node this controller's Luna has not got - never arrives, which is the
    one that wants different attention.
    """
    from trixd.core.replication import applied
    from trixd.plugins.prometheus import backend as prometheus

    def query(url, params=None, **_):
        """
        Prometheus, which has scraped nothing.
        """
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"result": []}})

    known = {f"node{index:03d}": {"hostname": f"node{index:03d}.cluster", "group": "compute"}
             for index in range(30)}
    backend = _prometheus_rig(tmp_path, monkeypatch, known)
    monkeypatch.setattr(prometheus.requests, "get", query)
    answer = backend.set_hw_rules({
        "nodes": [{"node": name, "force": True} for name in sorted(known)]
                 + [{"node": f"gone{index:03d}"} for index in range(30)]})

    # The precondition: sixty entries really declined, thirty for each cause, so the
    # sentence below is built from both of them and from one reason per node.
    reasons = [item.get("message") for item in answer["nodes"] if not item.get("ok")]
    assert len(reasons) == 60, f"the batch did not decline what this is about: {reasons}"
    assert len([one for one in reasons if "not a node Luna knows" in one]) == 30, reasons
    assert len(set(reasons)) == 31, f"the reasons are not one per node plus one: {reasons}"

    said = answer["skipped"]
    assert said == applied.declined(answer), \
        f"the sender cuts {len(said) - len(applied.declined(answer))} characters off a " \
        f"sentence of {len(said)} built past what it keeps"
    assert said.startswith("60 of 60"), f"the count no longer says how much: {said!r}"
    assert "not a node Luna knows" in said, \
        f"the cause shared by thirty nodes is crowded out by one sentence each: {said!r}"
    assert "has not been scraped yet" in said, said


def test_a_reason_longer_than_the_budget_does_not_take_the_count_with_it(rig, tmp_path,
                                                                        monkeypatch):
    """
    One reason can be longer than the whole budget on its own: a batch met by a Prometheus
    that is not answering declines with the transport error inside it. Built past the budget
    and cut at the end, that reason spends every character and the count of what did not fit
    is cut away with it - so the sender records a sentence stopping mid-word that says
    nothing about the second cause, which is the defect the budget was built against.
    """
    from trixd.core.replication import applied
    from trixd.plugins.prometheus import backend as prometheus

    def away(url, params=None, **_):
        """
        Prometheus, not answering, as requests reports one.
        """
        raise prometheus.requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='127.0.0.1', port=9): Max retries exceeded with url: "
            "/api/v1/query (Caused by NewConnectionError('<urllib3.connection."
            "HTTPConnection object: Failed to establish a new connection: [Errno 111] "
            "Connection refused'))")

    known = {f"node{index:03d}": {"hostname": f"node{index:03d}.cluster", "group": "compute"}
             for index in range(30)}
    backend = _prometheus_rig(tmp_path, monkeypatch, known)
    monkeypatch.setattr(prometheus.requests, "get", away)
    answer = backend.set_hw_rules({
        "nodes": [{"node": name, "force": True} for name in sorted(known)]
                 + [{"node": f"gone{index:03d}"} for index in range(10)]})

    # The preconditions, before the sentence is read: forty entries declined for two causes,
    # and the commoner of the two carries a reason longer than the whole budget.
    reasons = [item.get("message") for item in answer["nodes"] if not item.get("ok")]
    assert len(reasons) == 40, f"the batch did not decline what this is about: {reasons}"
    assert len([one for one in reasons if "not a node Luna knows" in one]) == 10, reasons
    assert len(max(reasons, key=len)) > applied.KEPT, \
        f"no reason here outruns the budget, so this proves nothing: {sorted(reasons)[0]}"

    said = answer["skipped"]
    assert said == applied.declined(answer), \
        f"the sender cuts {len(said) - len(applied.declined(answer))} characters off a " \
        f"sentence of {len(said)} built past what it keeps"
    assert said.endswith(prometheus.MORE.format(count=1)), \
        f"one long reason spent the budget and took the count of the rest with it: {said!r}"


def _sentence_makers(source: str) -> set:
    """
    The functions one module defines that say they answer a string.

    A handler may build its sentence in a helper beside it, and the annotation is the only
    statement in the tree of what comes back; one without an annotation is not read as a
    sentence, so this gains nothing by being guessed at.
    """
    import ast

    return {node.name for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(node.returns, ast.Name) and node.returns.id == "str"}


def _a_sentence(node, makers=frozenset()) -> bool:
    """
    Whether one expression can only be a string, given the sentence makers beside it.

    Conservative on purpose: an expression this cannot see through counts as not a
    sentence, because what it guards is a decline the sender never reads.
    """
    import ast

    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        # One string operand is enough: the other being a list is a TypeError at the
        # handler, not a decline that reads as applied at the sender.
        return _a_sentence(node.left, makers) or _a_sentence(node.right, makers)
    if isinstance(node, ast.IfExp):
        return _a_sentence(node.body, makers) and _a_sentence(node.orelse, makers)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("join", "format"):
            return True
        return isinstance(node.func, ast.Name) and (node.func.id == "str"
                                                    or node.func.id in makers)
    return False


def _skipped_answers(source: str) -> list:
    """
    Every `skipped` one module answers, as (function, line, the value expression).

    A dict literal, a dict(skipped=...) and an assignment into the key, which is every
    shape the tree writes one in.
    """
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Dict):
                found += [(node.name, value.lineno, value)
                          for key, value in zip(inner.keys, inner.values)
                          if isinstance(key, ast.Constant) and key.value == "skipped"]
            elif isinstance(inner, ast.keyword) and inner.arg == "skipped":
                found.append((node.name, inner.value.lineno, inner.value))
            elif isinstance(inner, ast.Assign) and any(
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "skipped" for target in inner.targets):
                found.append((node.name, inner.value.lineno, inner.value))
    return found


def test_a_sentence_is_told_from_a_list_before_the_tree_is_swept():
    """
    The sweep below is worth exactly what the reader behind it is worth: one that called
    every expression a string would pass over any tree at all, including the one this is
    here to refuse. Pinned in both directions, on the shapes a handler writes.
    """
    for text, sentence in (('{"skipped": "not in Luna"}', True),
                           ('{"skipped": f"{len(no)} of {len(all)}"}', True),
                           ('{"skipped": f"{len(no)} of {len(all)}: " + "; ".join(why)}', True),
                           ('{"skipped": str(left_out)}', True),
                           ('{"skipped": "left out: %s" % left_out}', True),
                           ('{"skipped": left_out}', False),
                           ('{"skipped": ["node001", "node002"]}', False),
                           ('{"skipped": len(left_out)}', False),
                           ('{"skipped": sorted(one["name"] for one in left_out)}', False),
                           ('answer["skipped"] = left_out', False),
                           ('dict(nodes=0, skipped=left_out)', False)):
        answers = _skipped_answers(f"def handler(answer):\n    {text}\n")
        assert len(answers) == 1, f"{text} was not read as one answer: {answers}"
        assert _a_sentence(answers[0][2]) is sentence, text

    # And a helper beside the handler, read through what it says it answers and no further:
    # a decline built in one is still a sentence, and one that says nothing is not.
    for says, sentence in (("-> str", True), ("-> list", False), ("", False)):
        module = (f"def why(left_out) {says}:\n    return str(left_out)\n\n"
                  f"def handler(answer):\n    return {{'skipped': why(answer)}}\n")
        answers = _skipped_answers(module)
        assert len(answers) == 1, f"{says} was not read as one answer: {answers}"
        assert _a_sentence(answers[0][2], _sentence_makers(module)) is sentence, says


def test_no_replicating_handler_answers_skipped_as_anything_but_a_sentence():
    """
    A decline reaches the sender through the reserved `skipped` string and nowhere else, so
    a handler answering a list or a count on a replicating endpoint declines where nothing
    can see it and the call is recorded as applied on both controllers. Both sides are read
    from the tree - which endpoints replicate from every shipped manifest, what their
    handlers answer from every module of those plugins - so the next endpoint to declare
    `replicate`, and the next handler to answer `skipped`, arrive here with nothing to
    remember. A handler building its sentence in a helper is read through what that helper
    says it answers, which is why a helper without an annotation reaches here as a refusal.
    """
    import ast
    import glob
    import os

    import yaml

    import trixd
    from trixd.core.plugins.manifest import parse

    manifests = sorted(glob.glob(os.path.join(
        os.path.dirname(trixd.__file__), "plugins", "*", "manifest.yaml")))
    assert manifests, "no shipped plugin was found, so this proves nothing"

    replicating = {}
    for path in manifests:
        with open(path, encoding="utf-8") as handle:
            declared = parse(yaml.safe_load(handle))
        handlers = {endpoint.handler for endpoint in declared.endpoints
                    if endpoint.replicate and endpoint.handler}
        if handlers:
            replicating[os.path.dirname(path)] = handlers
    assert replicating, "no shipped endpoint declares replicate, so this proves nothing"

    answers, offenders = [], []
    for directory, handlers in sorted(replicating.items()):
        for module in sorted(glob.glob(os.path.join(directory, "**", "*.py"),
                                       recursive=True)):
            with open(module, encoding="utf-8") as handle:
                source = handle.read()
            makers = _sentence_makers(source)
            for function, line, value in _skipped_answers(source):
                if function not in handlers:
                    continue
                answers.append(f"{function} at {module}:{line}")
                if not _a_sentence(value, makers):
                    offenders.append(f"{function} at {module}:{line} answers "
                                     f"skipped as {ast.unparse(value)}")
    assert answers, ("no replicating handler answers skipped at all, so this proves "
                     "nothing: either the reserved key went out of use or a handler is "
                     "no longer where its plugin's manifest says it is")
    assert offenders == [], ("a replicating handler declines where the sender cannot read "
                             "it:\n" + "\n".join(sorted(offenders)))

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


def test_a_declaration_the_route_would_ignore_is_refused_beside_its_neighbours():
    """
    Two more shapes of the same kind as the refusals they sit next to.

    A render that also declares a body schema, or a verb that is not a read: the reader
    behind a render is handed neither. The body is built, documented and checked, and then
    dropped on the way in; the write reaches that same reader, which answers what was last
    rendered. That is the path variable and the query parameter again, which are refused
    already. A public or forwarded endpoint that also takes a node token: neither is bound
    with a guard in front of it, so the token is never asked for and never bound to the node
    its path names - the route is open to anyone while the manifest says a node token. It is
    a path carrying one of the node names that gets here; one without is refused already,
    for the other half of the same reason.

    And a declared minimum on any of the three routes that cannot check one: the gate reads
    it on a guarded user-token route and nowhere else, so the manifest and the document a
    plugin author reads name a role that admits everyone.
    """
    from trixd.common.errors import PluginRefused
    from trixd.core.plugins import manifest as manifests

    base = {"plugin": "probe", "backend": "backend.Backend",
            "schemas": {"change": {"name": {"type": "string"}}}}
    for endpoints, because in (
            ([{"path": "renders/thing", "methods": ["GET"], "minimum": "none",
               "handler": "render_thing", "schema": "change",
               "render": {"inputs": ["luna.node"], "serve": "json"}}],
             "cannot also declare schema change"),
            ([{"path": "renders/thing", "methods": ["GET", "POST"], "minimum": "none",
               "handler": "render_thing",
               "render": {"inputs": ["luna.node"], "serve": "json"}}],
             "cannot also serve POST"),
            ([{"path": "metrics/<node>", "methods": ["GET"], "handler": "read",
               "public": True, "token": "node"}],
             "is public and takes a node token"),
            ([{"path": "metrics/<node>", "methods": ["GET"], "handler": "read",
               "forward": True, "token": "node"}],
             "is forward and takes a node token"),
            ([{"path": "metrics", "methods": ["GET"], "handler": "read",
               "public": True, "minimum": "manager"}],
             "is public and declares minimum manager"),
            ([{"path": "<path:rest>", "methods": ["GET"], "handler": "forward",
               "forward": True, "minimum": "manager"}],
             "is forward and declares minimum manager"),
            ([{"path": "rules/<node>", "methods": ["PATCH"], "handler": "write",
               "token": "node", "minimum": "manager"}],
             "is on a node token and declares minimum manager")):
        with pytest.raises(PluginRefused) as refusal:
            manifests.parse({**base, "endpoints": endpoints})
        assert because in str(refusal.value), (because, str(refusal.value))

    # And what these must not take with them, so neither can pass by refusing everything:
    # a schema on the write that declares it, a render that only reads, a public read, a
    # forwarded route on the token kind it does take, and a node token behind the gate -
    # the last three being the shapes that carry no minimum, which is what they may not
    # be refused for.
    sound = manifests.parse({**base, "endpoints": [
        {"path": "thing", "methods": ["PATCH"], "handler": "write", "schema": "change",
         "minimum": "manager"},
        {"path": "renders/thing", "methods": ["GET"], "minimum": "none",
         "handler": "render_thing",
         "render": {"inputs": ["luna.node"], "serve": "json"}},
        {"path": "metrics", "methods": ["GET"], "handler": "read", "public": True},
        {"path": "<path:rest>", "methods": ["GET"], "handler": "forward", "forward": True},
        {"path": "rules/<node>", "methods": ["PATCH"], "handler": "write",
         "token": "node"}]})
    assert len(sound.endpoints) == 5, "five shapes that have to go on loading"


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


def prometheus_module():
    """
    The prometheus backend module, for a test that needs its constants.
    """
    from trixd.plugins.prometheus import backend as prometheus

    return prometheus


def _hw_backend(tmp_path, monkeypatch, reload_ok=True, nodes=None):
    """
    A prometheus backend over tmp_path with one node, Prometheus answering one DIMM, and a
    reload that is counted.

    Luna holds node001, and whatever `nodes` adds, and answers None for every other name,
    as the real client does. A stub that hands a record back for any name asked resolves a
    node Luna has never held to node001's hostname, so a route that asks Luna reads as
    holding that node's rules and the test passes on a record Luna would not have given.
    """
    from types import SimpleNamespace

    from trixd.plugins.prometheus import backend as prometheus

    held = {"node001": {"hostname": "node001.cluster", "group": "compute"}, **(nodes or {})}

    class Luna:
        """
        The nodes Luna holds.
        """

        def node(self, name):
            """
            Its record, or None for a name Luna does not hold.
            """
            return held.get(name)

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


def _refusal_of(call) -> str:
    """
    What a call was refused with, or an empty string where it was not refused at all.
    """
    from trixd.common.errors import TrixdError

    try:
        call()
    except TrixdError as refusal:
        return refusal.message
    return ""


def test_a_rule_file_promtool_refuses_stays_refused_until_it_is_taken(rig, tmp_path,
                                                                      monkeypatch):
    """
    A file Prometheus's own checker will not take goes on being refused until it is right.

    promtool is asked once per reload, over the files written since the last one. Taken off
    that list before it had answered, a refusal left nothing behind: the next reload for any
    other node handed promtool only its own file and went ahead, Prometheus refused the
    directory it reads and kept its previous configuration, and the node whose file was bad
    was told its rules were in place every time it asked again.
    """
    from trixd.plugins.prometheus import backend as prometheus

    backend, _ = _hw_backend(tmp_path, monkeypatch,
                             nodes={"node002": {"hostname": "node002.cluster",
                                                "group": "compute"}})
    # promtool does not ship everywhere and is not installed here, so this path has never
    # run: without the stub the check is skipped and every reload below is taken.
    tool = tmp_path / "promtool"
    tool.write_text("", encoding="utf-8")
    monkeypatch.setattr(prometheus, "PROMTOOL", (str(tool),))
    bad = str(tmp_path / "trix.hw.node001.cluster.rules")
    refuses, checks, reloads = {bad}, [], []

    def run(argv, **_):
        """
        promtool over the files it is handed, or the reload command.
        """
        if argv[0] == str(tool):
            handed = list(argv[3:])
            checks.append(handed)
            named = sorted(set(handed) & refuses)
            return SimpleNamespace(ok=not named, out="",
                                   err=f"{named}: not a valid rule file" if named else "")
        reloads.append(argv)
        return SimpleNamespace(ok=True, out="", err="")

    monkeypatch.setattr(prometheus.shell, "run", run)

    told = _refusal_of(lambda: backend.import_hw_rules("node001", {}))
    assert checks and bad in checks[0], "promtool was not asked about the file just written"
    assert "promtool refuses" in told, "the refusal did not reach the node that caused it"
    assert not reloads, "Prometheus was reloaded over a file promtool refuses"

    # Any other node's write, which is what used to carry the refusal away with it.
    told = _refusal_of(lambda: backend.import_hw_rules("node002", {}))
    assert bad in checks[-1], (
        "another node's reload was checked over its own file alone, so the refused one was "
        "never named again while Prometheus went on refusing the directory it reads")
    assert "promtool refuses" in told and not reloads

    # The node's own retry. Nothing has changed on disk, so nothing is written - but the
    # check it is waiting on has still never passed, and it has to be told so.
    told = _refusal_of(lambda: backend.import_hw_rules("node001", {}))
    assert "promtool refuses" in told, (
        "asked again over a file promtool refuses, the node was answered that its rules "
        "are in place, with no reload attempted and nothing left saying otherwise")
    assert not reloads

    # Put right: the reload is taken, and what it covered stops being owed one.
    refuses.clear()
    assert backend.import_hw_rules("node001", {})["changed"] is False
    assert len(reloads) == 1, "the reload was never sent once promtool took the file"
    backend.import_hw_rules("node002", {})
    assert len(reloads) == 1, "a file promtool has taken is still being treated as pending"


def test_a_reload_that_failed_is_still_owed_when_the_writer_asks_again(rig, tmp_path,
                                                                      monkeypatch):
    """
    A write whose reload never landed is told so again, not that its rules are in place.

    promtool refusing and the reload command failing leave Prometheus in the same state -
    serving the configuration it held before the write - and what is owed a reload was put
    back for the first and dropped for the second. So the node that wrote took its 502,
    asked again, found its file unchanged and nothing outstanding, and was answered that
    its rules were in place while Prometheus had never read them.
    """
    from trixd.common.errors import BackendFailure

    backend, reloads = _hw_backend(tmp_path, monkeypatch, reload_ok=False)
    with pytest.raises(BackendFailure):
        backend.import_hw_rules("node001", {})
    path = tmp_path / "trix.hw.node001.cluster.rules"
    assert path.is_file() and len(reloads) == 1, (
        "nothing was written, or no reload was attempted over it, so there is no reload "
        "outstanding for the retry below to be told about")
    stamp = path.stat().st_mtime_ns

    try:
        again = backend.import_hw_rules("node001", {})
    except BackendFailure as refusal:
        again = refusal

    assert isinstance(again, BackendFailure), (
        f"asked again after a reload that failed, the node was answered {again} while "
        "Prometheus is still on the configuration it held before the write")
    assert path.stat().st_mtime_ns == stamp, (
        "the retry rewrote the file, so this says nothing about the write that finds "
        "nothing to do and is answered from the record alone")
    assert len(reloads) == 2, "the reload Prometheus never took was not attempted again"


def test_a_regenerate_leaves_one_live_rule_file_and_not_two(rig, tmp_path, monkeypatch):
    """
    Force writes the name Luna resolves now, and the file it found stops being loaded.

    Prometheus reads a glob, not a list of names, so a file left behind under the name a
    node answered to before is still loaded: both fire, and the one left behind goes on
    selecting the group the node has left. Retired rather than deleted, as every other
    route here retires a file it takes out of service.
    """
    import glob

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    # What a node's file is called when it was written before the hostname Luna holds for
    # it now, carrying a group it has since left.
    was = str(_hw_file(tmp_path, "node001", "stale"))
    assert backend._host_of("node001") == "node001.cluster", (
        "Luna resolves node001 to the name the planted file already carries, so a "
        "regenerate would write that same file and this would prove nothing")

    answer = backend.import_hw_rules("node001", {"force": True})

    assert answer["hostname"] == "node001.cluster"
    live = sorted(os.path.basename(found) for found in
                  glob.glob(os.path.join(str(tmp_path), "trix.hw.*.rules")))
    assert live == ["trix.hw.node001.cluster.rules"], (
        f"Prometheus loads this glob and it holds more than this node's one file: {live}")
    selecting = [name for name in live if 'luna_group="stale"' in
                 (tmp_path / name).read_text(encoding="utf-8")]
    assert not selecting, (
        f"a loaded rule file still selects the group the node has left: {selecting}")
    assert not os.path.isfile(was), "the file it found is still in the glob Prometheus reads"
    assert len(list((tmp_path / "backup").glob("trix.hw.node001.rules.*"))) == 1, (
        "the file it found was lost rather than retired")


def test_a_regenerate_writes_the_name_luna_resolves_even_when_the_rules_are_identical(
        rig, tmp_path, monkeypatch):
    """
    Identical content is not a reason to leave a node's rules under the old name.

    This is the ordinary case rather than a corner: a regenerate reads the same hardware and
    produces the same document byte for byte, and what has changed is only the name the file
    carries. Compared on the documents alone, force then reports the rules in order and
    writes nothing, so the node keeps answering under a name Luna no longer resolves and
    nothing moves it - which is also what would leave the file above unretired.
    """
    import shutil

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    target = tmp_path / "trix.hw.node001.cluster.rules"
    was = tmp_path / "trix.hw.node001.rules"
    # The document a regenerate produces, under the name the node answered to before: the
    # comparison has to look past content that is equal and a name that is not.
    shutil.move(str(target), str(was))

    answer = backend.import_hw_rules("node001", {"force": True})

    assert target.is_file(), (
        "force compared the documents alone and left this node's rules under a name Luna "
        "no longer resolves, with nothing written and nothing moved")
    assert answer["changed"] is True and answer["hostname"] == "node001.cluster"


def test_a_regenerate_whose_rules_are_identical_still_retires_the_duplicate(
        rig, tmp_path, monkeypatch):
    """
    Identical content is not a reason to leave a second file of this node's loaded.

    The unchanged answer is reached by the clause that says the hostname file exists, and a
    second file is found only when that one exists too - so both hold at once. A force that
    read back its own document returned saying the rules were in order, and the duplicate
    it was meant to take out of the glob stayed in it, selecting the group the node has
    left, for as long as nothing else rewrote the node.
    """
    import glob
    import shutil

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    live = tmp_path / "trix.hw.node001.cluster.rules"
    was = tmp_path / "trix.hw.node001.rules"
    # Both names on disk with the same document: the one Luna resolves now, and the one the
    # node answered to before a rename. A regenerate reads the same hardware and produces
    # what is already in both of them, which is the ordinary case rather than a corner.
    shutil.copy2(str(live), str(was))
    assert was.read_text(encoding="utf-8") == live.read_text(encoding="utf-8"), (
        "the two files are not on disk with the same content, so neither the unchanged "
        "answer nor the retire that has to happen anyway is reached")

    answer = backend.import_hw_rules("node001", {"force": True})

    loaded = sorted(os.path.basename(found) for found in
                    glob.glob(os.path.join(str(tmp_path), "trix.hw.*.rules")))
    assert loaded == ["trix.hw.node001.cluster.rules"], (
        f"Prometheus loads this glob and it still holds a duplicate of this node: {loaded}")
    assert len(list((tmp_path / "backup").glob("trix.hw.node001.rules.*"))) == 1, (
        "the duplicate was lost rather than retired")
    assert answer["changed"] is True, (
        "a file was taken out of the glob Prometheus loads, so something did change")


def test_callers_waiting_together_share_a_reload_and_none_gets_an_earlier_one(rig):
    """
    The two halves of the contract, driven rather than timed.

    A reload covers the tickets taken before it started, so one caller that arrives first
    gets its own and everyone who asks while it runs shares the next - thirty callers, two
    reloads, and not one of the twenty-nine answered by the reload that was already running
    when they asked, because their files were not on disk yet.

    The overlap is arranged with an event instead of a sleep. Asserting a count of reloads
    after starting thirty threads and hoping they overlap tests how the machine scheduled
    them: on a loaded box they run one after another, each gets its own reload, and the
    test fails having found nothing wrong.
    """
    import threading

    from trixd.plugins.prometheus.backend import SharedReload

    shared = SharedReload()
    gate = threading.Event()
    running = threading.Event()
    names = []

    def held_reload():
        """
        A reload that does not finish until the test lets it, and says which run it was.
        """
        names.append(f"run-{len(names) + 1}")
        running.set()
        gate.wait(30)
        return True, names[-1]

    answers = {}

    def caller(number):
        """
        One writer asking for a reload after its own write.
        """
        answers[number] = shared.run(held_reload)

    first = threading.Thread(target=caller, args=(0,))
    first.start()
    assert running.wait(30), "the first caller never reached the reload, so nothing overlaps"

    rest = [threading.Thread(target=caller, args=(number,)) for number in range(1, 30)]
    for thread in rest:
        thread.start()
    waiting = _until(lambda: _issued(shared) == 30)
    assert waiting, f"only {_issued(shared)} of 30 callers had taken a ticket"

    gate.set()
    first.join(30)
    for thread in rest:
        thread.join(30)

    assert len(answers) == 30 and all(ok for ok, _ in answers.values()), answers
    assert shared.runs == 2, f"{shared.runs} reloads where the first covers only itself"
    assert answers[0][1] == "run-1", answers[0]
    served = {number: outcome for number, outcome in answers.items() if number}
    assert all(said == "run-2" for _, said in served.values()), (
        "somebody was answered by the reload that was already running when they asked: "
        f"{sorted(set(said for _, said in served.values()))}")


def _issued(shared) -> int:
    """
    How many tickets have been taken, read under the lock the counter lives behind.
    """
    with shared.condition:
        return shared.issued


def _until(settled, seconds: float = 30.0) -> bool:
    """
    Whether something became true inside the deadline, polled rather than slept through.
    """
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if settled():
            return True
        time.sleep(0.01)
    return settled()


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


def test_a_deviation_written_as_nothing_switches_the_exporter_off(rig, tmp_path):
    """
    `lshw:` with nothing under it is off, exactly as `false` is.

    yaml reads a key with nothing under it as null, and a scope stored as JSON carries a
    null the same way, so this is what an operator who clears a value leaves behind. Read
    instead as a value this does not understand, it warns and keeps the exporter as the
    baseline has it - on, in the one place somebody had just switched it off.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))
    rows = {"group:gpu": {"lshw": None}}

    resolved = exporters.for_node("node001", "gpu", baseline, {}, rows)

    assert "lshw" not in resolved, "a deviation of null left the exporter on"
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

    The cluster scope moves an interval on `node`, the one exporter both baselines carry, because
    a deviation on a name the controllers baseline lacks is a no-op there by construction - it
    could leak for years and this would still pass. A node is resolved from the same rows as
    well, so the row is known to do something on the ladder it was written for.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))
    rows = {"cluster": {"node": {"interval": "9m"}},
            "controllers": {"ha": {"interval": "1m"}},
            "ctrl2.cluster": {"ha": False}}

    one = exporters.for_controller("ctrl1.cluster", baseline, {}, rows)
    two = exporters.for_controller("ctrl2.cluster", baseline, {}, rows)
    on_a_node = exporters.for_node("node001", "compute", baseline, {}, rows)

    assert one["ha"]["interval"] == "1m", "the controllers scope did not apply"
    assert "ha" not in two, "a controller's own row did not override the controllers scope"
    assert "node" in one and "node" in two, "the controllers baseline lost its own node exporter"
    assert on_a_node["node"]["interval"] == "9m", \
        "the cluster scope did not reach a node, so a leak onto a controller would not show here"
    for hostname, resolved in (("ctrl1.cluster", one), ("ctrl2.cluster", two)):
        assert resolved["node"].get("interval") is None, \
            f"the cluster scope leaked onto {hostname}: its node exporter carries the interval " \
            f"{resolved['node'].get('interval')!r} that was written for every node"
        assert "lshw" not in resolved, \
            f"an exporter from the nodes baseline was scraped on {hostname}"


def test_a_rule_written_for_every_controller_does_not_reach_a_node(rig, tmp_path):
    """
    The same separation in the other direction: the controllers scope and a controller's own row
    are read on the controller ladder alone, so neither reaches one of Luna's nodes.

    Both rows move a timeout on `node`, the one exporter both baselines carry - a name the nodes
    baseline lacks would arrive on a node without a port and be dropped by the render, so it
    would leak in silence. The controller's own row is keyed on a bare hostname, which is how a
    controller writes its scope, and it is given the node's own name so that a ladder reading the
    wrong row has somewhere to land.
    """
    from trixd.plugins.prometheus import exporters

    baseline = exporters.factory(_factory_file(tmp_path))
    rows = {"controllers": {"node": {"timeout": "11s"}},
            "node001": {"node": {"timeout": "12s"}}}

    assert exporters.for_controller("node001", baseline, {}, rows)["node"]["timeout"] == "12s", \
        "neither controller row applied, so a leak onto a node would not show here"

    resolved = exporters.for_node("node001", "compute", baseline, {}, rows)

    assert resolved["node"].get("timeout") is None, \
        "a rule written for a controller reached a node: its node exporter carries the " \
        f"timeout {resolved['node'].get('timeout')!r}"


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


def test_an_exporter_with_no_port_cannot_take_the_whole_document_down(rig, tmp_path,
                                                                      monkeypatch):
    """
    An entry with no port is left out of the render, not rendered without one.

    A target is an address and a port, and the render reads that port directly, so an entry
    reaching it without one raises - and a raise there is not one exporter missing but the
    whole document: http_sd is answered nothing and every node on the cluster leaves the
    scrape pool at once. Two ordinary things produce such an entry, and neither is refused
    where it is read: a port in the file that is not a number, which is dropped while the
    rest of the entry is kept, and a scope naming an exporter nothing has given a port.
    """
    from trixd.utils import declarations

    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "compute"}})
    backend.config({"exporters_file": _factory_file(tmp_path, {
        "nodes": {"node": {"port": "14200"}, "lshw": {"port": 14208}},
        "controllers": {"ha": {"port": 14205}},
        "hosts": {"controller1": {"address": "10.141.255.254"}}})})
    declarations.write("prometheus", "exporter-scope", "cluster",
                       {"telegraf": {"interval": "30s"}})

    try:
        document = backend.render_targets()
    except Exception as error:                      # pylint: disable=broad-except
        # Named here rather than left to the traceback: what this pins is that the render
        # survives such an entry at all, so the failure has to say so rather than read as
        # an accident of the harness.
        pytest.fail(f"an exporter with no port took service discovery down: {error!r}")

    assert sorted(entry["labels"]["exporter"] for entry in document) == ["ha", "lshw"], (
        "service discovery lost the exporters that do have a port, or rendered one that "
        "has none")
    assert all(":" in entry["targets"][0] and not entry["targets"][0].endswith(":")
               for entry in document), "a target was rendered without a port"


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


def test_two_scope_changes_at_one_version_keep_both_exporters(rig, tmp_path, monkeypatch,
                                                              interleave_once):
    """
    The exporter routes read in one session and wrote in another, so two operators changing
    different exporters of one scope each kept their own and dropped the other's - each
    answered 200 with the deviation it sent, only one of which was stored.
    """
    backend = _prometheus_rig(tmp_path, monkeypatch, {})

    fired = interleave_once(lambda: backend.set_applied("cluster",
                                                        {"exporters": {"ipmi": False}}))

    answer = backend.set_applied("cluster", {"exporters": {"node": True}})

    assert fired, "the other change never ran, so nothing interleaved"
    assert answer["exporters"].get("ipmi") is False, \
        "the change that landed first was overwritten, and its caller was answered 200"
    assert answer["exporters"].get("node") is True, \
        "the change that won lost its own exporter"


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


def test_a_brace_inside_a_label_value_is_part_of_the_value(rig, tmp_path, monkeypatch):
    """
    What the write-time check must not refuse. A brace and a comma are ordinary characters
    inside a quoted value - {3,} in a matcher's regex is a bounded repetition, not the end
    of the selector - so a check that reads to the first closing brace sees the rest of the
    value as text outside every matcher and refuses legitimate PromQL. These four have to
    be written.

    Both directions in one test, because a check that accepts everything reads exactly like
    a check that works: the value that closes its quote early is what this one exists for,
    and it is still refused.
    """
    from trixd.common.errors import BackendFailure
    from trixd.plugins.prometheus import backend as prometheus

    legitimate = ['node_filesystem_avail_bytes{mountpoint=~"/var/lib/.{3,}"} < 1e9',
                  'up{instance=~"node00{1,3}"} == 0',
                  'absent(lshw_device{product="Mem{DDR4}"})',
                  'rate(http_requests_total{code="200"}[5m]) > 0']
    for expr in legitimate:
        assert prometheus.unaccounted(expr) == "", f"{expr} leaves text no matcher accounts for"

    # The write path itself, not only the check: the document reaches the file.
    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    path = os.path.join(str(tmp_path), prometheus.GENERIC_RULES)
    backend._write_rules(path, {"groups": [{"name": "generic", "rules": [
        {"alert": f"A{number}", "expr": expr}
        for number, expr in enumerate(legitimate)]}]})
    held = open(path, encoding="utf-8").read()
    assert "node00{1,3}" in held, f"a legitimate document was not written: {held}"

    # An inch mark in a product string, interpolated without the escape: the rest of the
    # value is left outside every matcher, which is the break the check exists for.
    with pytest.raises(BackendFailure) as refused:
        backend._write_rules(path, {"groups": [{"name": "generic", "rules": [
            {"alert": "B",
             "expr": 'absent(lshw_device{product="2.5" SATA SSD",vendor="V"})'}]}]})
    said = refused.value.message
    assert 'SATA SSD"' in said, said
    assert prometheus.GENERIC_RULES in said, said
    # Only what is true: text outside the matchers says a value is not quoted as it was
    # meant to be, and says nothing about what Prometheus does with the file it is in.
    assert "refuse the whole file" not in said, said
    assert open(path, encoding="utf-8").read() == held, "the refused document was written"


def test_a_refused_rule_document_is_not_stored(rig, tmp_path, monkeypatch):
    """
    The declaration was written before the documents were rendered, so a PATCH answered 500
    stored the document anyway. From there it replicates to the peer, and every later
    render raises on it before reaching the per-node hardware reconcile underneath - on
    both controllers, until someone sends a rule document again. The render engine keeps
    the previous output and logs that it did, so nothing says the reconcile has stopped.
    """
    from trixd.common.errors import BackendFailure

    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node002": {"hostname": "node002.cluster",
                                           "group": "compute"}})
    kept = {"groups": [{"name": "generic", "rules": [
        {"alert": "A", "expr": 'absent(lshw_device{hostname="node002.cluster"})'}]}]}
    backend.rules(body=kept)

    with pytest.raises(BackendFailure):
        backend.rules(body={"groups": [{"name": "generic", "rules": [
            {"alert": "B", "expr": 'absent(lshw_device{product="2.5" SATA SSD"})'}]}]})

    assert backend.rules() == kept, "the refused document was stored"

    # And a render reaches the hardware files: one naming a node Luna no longer has is
    # retired, which is the reconcile the stored document used to raise in front of.
    _hw_file(tmp_path, "node001.cluster", "compute")
    outcome = backend.render_rule_files()
    assert outcome["retired"] == ["trix.hw.node001.cluster.rules"], outcome


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


def test_a_plugin_nobody_enabled_plugs_nothing_in(tmp_path, monkeypatch):
    """
    Another plugin reaching for the Luna client imports the luna package on the way, and
    that import used to register the node source and start the Luna poller. So a daemon with
    luna in neither ENABLED nor the plugin directory still polled Luna and still minted
    provision-scoped node tokens, while /system/plugins named neither - and nothing on the
    control surface could stop it, because the import resolves against the Python package
    path rather than the configured directory.

    Anything a plugin offers the daemon belongs in its install(), which the loader calls
    once the plugin has loaded. Asserted on the registries rather than through a rig,
    because what is under test is the import itself.
    """
    import importlib
    import sys

    from trixd.core.render import triggers
    from trixd.core.security import nodes

    # A fresh import of both, with the registries empty, is the condition a daemon starts
    # in. Without dropping them the modules are already imported by an earlier test and the
    # side effect - if it came back - would have happened long ago.
    for module in [name for name in list(sys.modules)
                   if name.startswith("trixd.plugins.luna")
                   or name.startswith("trixd.plugins.prometheus")]:
        sys.modules.pop(module, None)
    monkeypatch.setattr(nodes, "_source", None)
    monkeypatch.setattr(nodes, "_owner", "")
    standing = set(triggers.states())

    importlib.import_module("trixd.plugins.prometheus.backend")

    assert nodes._source is None, \
        "importing another plugin's backend registered a node source, so a daemon that " \
        "never loaded luna would still issue node tokens"
    assert set(triggers.states()) - standing == set(), \
        f"it also started {sorted(set(triggers.states()) - standing)}"


OTHER_NODE_HOST = "node001.aaa"
OTHER_NODE_FILE = f"trix.hw.{OTHER_NODE_HOST}.rules"
OTHER_NODE_MARKER = "a_different_node_owns_this"


def _plant_a_short_name_twin(backend):
    """
    A rule file belonging to a *different* node that happens to share node001's short name.

    Two nodes whose hostnames differ only past the first dot have the same short name, which
    is what makes a bare short name ambiguous. It sorts before node001's own file, so any
    entry point that takes the first match takes this one.
    """
    import os

    import yaml

    path = os.path.join(backend._rules_dir(), OTHER_NODE_FILE)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump({"groups": [{"name": OTHER_NODE_MARKER, "rules": [
            {"alert": "SomethingElse", "expr": f'up{{hostname="{OTHER_NODE_MARKER}"}} == 0',
             "labels": {"hostname": OTHER_NODE_MARKER, "luna_group": "other"}}]}]}, handle)
    return path


def _rule_file_digests(backend):
    """
    Every rule file's full contents, live and retired, by name.

    The whole file, not the alert expressions: a relabel rewrites only `labels`, so anything
    comparing expressions alone cannot see one node's file being rewritten for another.
    """
    import hashlib
    import os

    directory = backend._rules_dir()
    found = {}
    for root, _, names in os.walk(directory):
        for name in names:
            path = os.path.join(root, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, directory)] = hashlib.sha256(handle.read()).hexdigest()
    return found


def _hw_rules_entry_points(method: str = ""):
    """
    Every caller-facing way into the hardware rule files, derived from the manifest.

    Returned as {handler: how_to_call}. Derived rather than listed so an endpoint or hook
    added to the manifest arrives here without a file to remember to edit - the test below
    fails naming it until it is either asserted or excluded on purpose.

    With a method, only the endpoints that take it, which leaves the hooks out: the readers
    are the endpoints answering GET.
    """
    import os

    import yaml

    from trixd.plugins import prometheus

    manifest = yaml.safe_load(
        open(os.path.join(os.path.dirname(prometheus.__file__), "manifest.yaml"),
             encoding="utf-8"))
    handlers = {entry["handler"] for entry in manifest["endpoints"]
                if str(entry.get("path", "")).startswith("hw-rules")
                and (not method or method in (entry.get("methods") or []))}
    if not method:
        handlers |= set((manifest.get("hooks") or {}).values())
    return handlers


# How each one is asked about node001 by its bare short name, which both files answer to.
# import_hw_rules is called twice: force decides whether the file found is rewritten or a
# fresh one generated, and only the first of those reads the file it found.
ASK_BY_SHORT_NAME = {
    "get_hw_rules": [lambda api: api.get_hw_rules("node001")],
    "list_hw_rules": [lambda api: api.list_hw_rules(
        __import__("types").SimpleNamespace(query_params={"nodes": "node001"}))],
    "import_hw_rules": [lambda api: api.import_hw_rules("node001", {}),
                        lambda api: api.import_hw_rules("node001", {"force": True})],
    "set_hw_rules": [lambda api: api.set_hw_rules({"nodes": [{"node": "node001"}]})],
    "repair_hw_rules": [lambda api: api.repair_hw_rules("node001")],
    "rename_hw_rules": [lambda api: api.rename_hw_rules("node001", {"was": "node001"})],
    "remove_hw_rules": [lambda api: api.remove_hw_rules("node001")],
    "node_updated": [lambda api: api.node_updated("node001")],
    "node_deleted": [lambda api: api.node_deleted("node001")],
}

# _reconcile_hw_rules is deliberately not among them. It takes no name from a caller: it
# works from Luna's whole node list, and a file sharing a short name with a node Luna holds
# is treated there as the name that node answered to before a rename, so discarding it is
# that pass's documented job. Its own ambiguity guard is the node list, and it is covered
# where two nodes in Luna share one short name.
NOT_ASKED_BY_NAME = {"_reconcile_hw_rules"}


def test_no_hardware_rules_route_touches_a_node_that_merely_shares_a_short_name(
        rig, tmp_path, monkeypatch):
    """
    A bare short name that two rule files answer to is refused, on every route, not served.

    Two nodes whose hostnames differ only past the first dot share a short name. Taking the
    first match is how one node's rules were read, rewritten and retired because somebody
    asked about another - and the reconcile pass was taught not to do it while every other
    entry point still did. Enumerated from the manifest so the next route added has to say
    which it is.

    `import_hw_rules` with force is the one row that was already right: force never writes
    to the file it matched, only to the name Luna resolves, so the match cannot send it
    astray. It is asserted anyway - the route is in the class, and that has to stay true.
    """
    import glob

    from trixd.common.errors import NotFound, TrixdError

    declared = _hw_rules_entry_points()
    covered = set(ASK_BY_SHORT_NAME) | NOT_ASKED_BY_NAME
    assert declared <= covered, (
        f"the manifest declares {sorted(declared - covered)} as a way into the hardware "
        f"rules, and this test does not ask it by a short name: add it to "
        f"ASK_BY_SHORT_NAME, or to NOT_ASKED_BY_NAME with the reason")

    damage = []
    for handler, calls in sorted(ASK_BY_SHORT_NAME.items()):
        for index, call in enumerate(calls):
            directory = tmp_path / f"{handler}-{index}"
            directory.mkdir()
            backend, _ = _hw_backend(directory, monkeypatch)
            backend.import_hw_rules("node001", {})
            twin = _plant_a_short_name_twin(backend)
            assert backend._host_of("node001") == "node001.cluster", (
                "Luna does not resolve the bare short name here, so this proves nothing")
            assert os.path.basename(sorted(glob.glob(
                os.path.join(backend._rules_dir(), "trix.hw.*.rules")))[0]) == OTHER_NODE_FILE, (
                "the other node's file no longer sorts first, so taking the first match "
                "would take the right one by luck and this would pass either way")
            before = _rule_file_digests(backend)

            try:
                answer = repr(call(backend))
            except (NotFound, TrixdError) as refusal:
                answer = repr(refusal)

            after = _rule_file_digests(backend)
            moved = {name for name in set(before) | set(after)
                     if before.get(name) != after.get(name)}
            # Collected rather than asserted one at a time: a resolver that goes wrong
            # goes wrong on several routes at once, and the first one to fail says the
            # least about how far it reaches.
            if OTHER_NODE_FILE in moved:
                damage.append(f"{handler}[{index}] rewrote or retired {OTHER_NODE_FILE}")
            if not os.path.isfile(twin):
                damage.append(f"{handler}[{index}] retired another node's rule file")
            # Two ways it shows in an answer: the document itself, and the host a write
            # reports back. A write that finds the wrong file and rewrites it unchanged
            # moves no byte and reports that node001's rules are in order - naming the
            # other node's host as it does so, which is the only trace left.
            if OTHER_NODE_MARKER in answer or OTHER_NODE_HOST in answer:
                damage.append(f"{handler}[{index}] answered for {OTHER_NODE_HOST}: "
                              f"{answer[:120]}")

    assert not damage, ("asked about node001 by a short name two rule files answer to, "
                        "these touched or served the other node's:\n  " + "\n  ".join(damage))


def test_listing_the_hardware_rules_answers_for_the_whole_estate_by_default(
        rig, tmp_path, monkeypatch):
    """
    No arguments means every node held, each with its document.

    There are consumers that need the estate in one answer, so the full document for every
    node is the contract rather than an oversight. Narrowing the default - to counts, or to
    a page - would not fail for them: they would get a thinner answer and no error, which is
    the worst way for this to break. Pinned here because the size of that answer reads like
    a defect to anyone meeting it for the first time, and it is not one.
    """
    import glob
    from types import SimpleNamespace

    from trixd.plugins.prometheus.backend import _hw_host

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    _plant_a_short_name_twin(backend)
    held = sorted(_hw_host(path) for path in
                  glob.glob(os.path.join(backend._rules_dir(), "trix.hw.*.rules")))

    answer = backend.list_hw_rules(SimpleNamespace(query_params={}))["nodes"]

    assert [entry["node"] for entry in answer] == held, "not every node held was answered for"
    assert all(entry["exists"] and "document" in entry and entry["document"] for entry in answer), (
        "the default answer no longer carries each node's rule file")


def _get_says_held(backend, node: str) -> bool:
    """
    Whether the route for one node serves a rule file for it.
    """
    from trixd.common.errors import NotFound

    try:
        return bool(backend.get_hw_rules(node))
    except NotFound:
        return False


def _list_says_held(backend, node: str) -> bool:
    """
    Whether the listing says this node's rules are held.
    """
    from types import SimpleNamespace

    answer = backend.list_hw_rules(SimpleNamespace(query_params={"nodes": node}))["nodes"]
    return bool(answer[0]["exists"])


# How each reader is asked whether one node's rules are held, keyed by its handler so the
# manifest decides which readers the test below covers.
HELD_BY_READER = {"get_hw_rules": _get_says_held, "list_hw_rules": _list_says_held}


def test_every_reader_agrees_whether_a_node_holds_hardware_rules(rig, tmp_path, monkeypatch):
    """
    One node, one question, one answer, whichever reader is asked it.

    A node's rule files are the ones Luna names for it, and a node whose name and hostname
    differ in their short form answers to neither by file name alone. A reader resolving by
    file name says nothing is held for it while the file sits on disk and the other reader
    serves that same file - which reaches a caller as a node that both has and has not got
    rules, and sends a manager regenerating what is already there. Enumerated from the
    manifest rather than named, so a reader added to it arrives here and has to answer.
    """
    readers = _hw_rules_entry_points(method="GET")
    assert readers <= set(HELD_BY_READER), (
        f"the manifest declares {sorted(readers - set(HELD_BY_READER))} as a way to read "
        f"the hardware rules, and this test does not ask it whether a node's rules are "
        f"held: add it to HELD_BY_READER")

    backend, _ = _hw_backend(tmp_path, monkeypatch, nodes={
        "node042": {"hostname": "sled07.cluster", "group": "compute"}})
    backend.import_hw_rules("node042", {})
    held_file = prometheus_module().HW_RULES.format(host="sled07.cluster")
    assert os.path.isfile(os.path.join(backend._rules_dir(), held_file)), (
        "nothing was written under the hostname, so there is no resolution to disagree on")
    assert "node042" not in held_file, (
        "the file name carries the node's own name, so a reader resolving by file name "
        "finds it as well and this would pass either way")

    answers = {name: ask(backend, "node042") for name, ask in sorted(HELD_BY_READER.items())}

    assert set(answers.values()) == {True}, (
        f"node042's rule file is on disk as {held_file}, and the readers do not agree that "
        f"it is held: {answers}")

    # A name Luna does not hold, that no file carries either: not held, on every reader.
    unheld = {name: ask(backend, "node999") for name, ask in sorted(HELD_BY_READER.items())}
    assert set(unheld.values()) == {False}, (
        f"Luna holds no node999 and no file names one, and the readers answer: {unheld}")


def _plugin_whose_install_fails(tmp_path, name: str, before: str = "",
                                raising: bool = True) -> None:
    """
    A plugin on disk that loads, whose install() runs `before` and then raises.

    Called again for the same name it rewrites install() alone, so a test can load a
    version that plugs itself in - `raising` false - and then reload one that will not.
    """
    from trixd.core.plugins import scaffold

    if not os.path.isdir(tmp_path / "plugins" / name):
        scaffold.new(name, str(tmp_path / "plugins"))
    (tmp_path / "plugins" / name / "__init__.py").write_text(
        'class Poll:\n    """A trigger that is started, stopped and asked its state."""\n\n'
        '    def state(self) -> dict:\n        """What health would publish."""\n'
        '        return {"answering": True}\n\n'
        '    def start(self) -> None:\n        """Asked to begin."""\n\n'
        '    def shutdown(self) -> None:\n        """Asked to stop."""\n\n\n'
        'class Source:\n    """A node source the sign-in route can ask about a node."""\n\n'
        '    def node(self, name: str) -> dict:\n'
        '        """Not a node this one knows, which is an answer rather than a refusal."""\n'
        '        return {}\n\n\n'
        'def install() -> None:\n'
        '    """Plug in, as far as it gets."""\n'
        f'{before}'
        + ('    raise RuntimeError("this plugin cannot plug itself in")\n' if raising
           else '    return None\n'),
        encoding="utf-8")


def test_a_plugin_whose_install_fails_is_not_left_serving(rig, client, headers, tmp_path):
    """
    install() ran after the routes had been exchanged and the registry already said
    loaded, so a plugin that refused was serving every route it declared while the caller
    was told it had not loaded.
    """
    from trixd.common.constants import PLUGIN_STATE_LOADED, PLUGIN_STATE_REFUSED
    from trixd.common.errors import PluginRefused
    from trixd.core.plugins import loader
    from trixd.core.plugins.registry import current as registry

    _plugin_whose_install_fails(tmp_path, "refuser")

    with pytest.raises(PluginRefused, match="could not plug itself in"):
        loader.load(rig, "refuser")

    serving = [route.path for route in rig.router.routes
               if getattr(route, "path", "").startswith(f"{API_PREFIX}/refuser")]
    assert serving == [], f"refused and serving {serving}"
    assert registry().get("refuser").state != PLUGIN_STATE_LOADED, \
        "the registry says loaded, so its hooks are dispatched and a reload is skipped"
    shown = {entry["name"]: entry["state"] for entry
             in client.get(f"{API_PREFIX}/system/plugins", headers=headers).json()["plugins"]}
    assert shown.get("refuser") == PLUGIN_STATE_REFUSED, shown


def test_an_install_that_fails_part_way_leaves_nothing_registered(rig, tmp_path, monkeypatch):
    """
    install() is a sequence - luna offers a trigger, then a node source, then its
    documentation link - so a failure in the middle of it left what came before behind:
    a trigger polling, and a node source answering sign-ins, for a refused plugin.
    """
    from trixd.common.errors import PluginRefused
    from trixd.core.plugins import loader
    from trixd.core.render import triggers
    from trixd.core.security import nodes

    # Put back afterwards: this install takes the node source over from the rig's own luna
    # plugin before it fails, and nothing else restores it between tests.
    monkeypatch.setattr(nodes, "_source", nodes._source)
    monkeypatch.setattr(nodes, "_owner", nodes._owner)

    _plugin_whose_install_fails(tmp_path, "halfway", before=(
        "    from trixd.core.render import triggers\n"
        "    from trixd.core.security import nodes\n"
        '    triggers.register("halfway_poll", Poll(), owner="halfway")\n'
        '    nodes.register(object(), owner="halfway")\n'))

    with pytest.raises(PluginRefused, match="could not plug itself in"):
        loader.load(rig, "halfway")

    assert "halfway_poll" not in triggers.states(), "its trigger is still polling"
    assert nodes._owner != "halfway", "its node source still answers a node sign-in"


def test_a_reload_refused_by_install_leaves_the_trigger_and_source_it_found(
        rig, client, headers, tmp_path, monkeypatch):
    """
    Both registries install() writes to key on the plugin's name rather than the version,
    and the purge before the import does not touch either - so the rollback above used to
    take the trigger and node source of the version that goes on serving. Its routes
    answered and the plugin read as loaded with its poller stopped and a node sign-in
    refusing for want of a source, and health published nothing wrong.
    """
    from trixd.common.constants import PLUGIN_STATE_LOADED
    from trixd.common.errors import PluginRefused
    from trixd.core.plugins import loader
    from trixd.core.plugins.registry import current as registry
    from trixd.core.render import triggers
    from trixd.core.security import nodes

    # Put back afterwards: this install takes the node source over from the rig's own luna
    # plugin, and nothing else restores it between tests.
    monkeypatch.setattr(nodes, "_source", nodes._source)
    monkeypatch.setattr(nodes, "_owner", nodes._owner)

    offers = ("    from trixd.core.render import triggers\n"
              "    from trixd.core.security import nodes\n"
              '    triggers.register("settled_poll", Poll(), owner="settled")\n'
              '    nodes.register(Source(), owner="settled")\n')
    _plugin_whose_install_fails(tmp_path, "settled", before=offers, raising=False)
    loader.load(rig, "settled")
    assert "settled_poll" in triggers.states(), \
        "it plugged no trigger in, so there is nothing here to lose"
    assert nodes._owner == "settled", "it offered no node source, so there is none to lose"

    # The same plugin again, refusing before it registers anything: so whatever is still
    # registered below can only be what the version that is serving put there.
    _plugin_whose_install_fails(tmp_path, "settled")
    with pytest.raises(PluginRefused, match="could not plug itself in"):
        loader.load(rig, "settled")

    entry = registry().get("settled")
    assert entry.state == PLUGIN_STATE_LOADED and entry.routes, \
        "the old version stopped serving, so there is nothing here to keep"
    assert "settled_poll" in triggers.states(), \
        "the refused reload stopped the trigger of a version that is still serving"
    assert nodes._owner == "settled", \
        "the refused reload dropped the node source of a version that is still serving"
    answer = client.post(f"{API_PREFIX}/auth/node/node001",
                         json={"username": "tester", "password": "secret"})
    assert answer.status_code != 503, f"a node sign-in has no source to ask: {answer.text}"


def test_every_registry_the_install_rollback_reaches_is_guarded_on_a_reload():
    """
    The rollback in _install keys on the plugin's name, which on a reload is the name of the
    version that goes on serving - so everything it takes back has to sit behind the branch
    that asks whether one does. A carrier added beside the trigger and the node source
    inherits that from the guard, and is a reload's defect again the moment it sits outside
    it; the documentation placeholder, which has no inverse at all today, is the next one.

    Derived rather than listed, because an example test has to be remembered: every forget
    the rollback calls is read out of the loader's own syntax tree.
    """
    import ast

    from trixd.core.plugins import loader

    with open(loader.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    install = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "_install")
    assert "serving" in {item.arg for item
                         in install.args.args + install.args.kwonlyargs}, \
        "_install is not told whether a version is serving, so nothing here can be guarded"
    rollback = next(node for node in ast.walk(install)
                    if isinstance(node, ast.ExceptHandler))

    parents = {}
    for node in ast.walk(rollback):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def _asks_whether_serving(node) -> bool:
        """
        Whether this call sits inside a branch that reads whether a version is serving.
        """
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If) and any(
                    isinstance(item, ast.Name) and item.id == "serving"
                    for item in ast.walk(node.test)):
                return True
        return False

    forgets = [node for node in ast.walk(rollback) if isinstance(node, ast.Call)
               and getattr(node.func, "attr", "") == "forget"]
    assert forgets, "the rollback takes nothing back, so this test checks nothing"
    unguarded = sorted(ast.unparse(node) for node in forgets
                       if not _asks_whether_serving(node))
    assert unguarded == [], (
        f"{', '.join(unguarded)} runs on a refused reload too, and that registry keys on "
        f"the plugin's name: it takes the registration of the version still serving")


def test_a_repair_relabels_the_live_file_when_an_older_one_sits_beside_it(rig, tmp_path,
                                                                          monkeypatch):
    """
    Two files for the SAME node: one under the name it answers to now, one under the name it
    was written under before. Both are this node's, which is what makes this different from
    the short-name case next door, where the second file belongs to a different node.

    The older one is discarded, and the live one is still relabelled. Resolving a node to a
    file answers on the name asked for alone when a file carries it, and a repair asks by the
    node's name while files are written under its hostname - so the live file was not looked
    at at all, the older one was discarded as superseded, and a relabel that was due did not
    happen. The expression is what the alerts match on, so the node's hardware alerting goes
    on matching a group it has left, and the answer says `rewritten: []` rather than saying so.
    """
    import yaml

    backend, _ = _hw_backend(tmp_path, monkeypatch)
    backend.import_hw_rules("node001", {})
    directory = backend._rules_dir()
    live = os.path.join(directory, prometheus_module().HW_RULES.format(host="node001.cluster"))
    older = os.path.join(directory, prometheus_module().HW_RULES.format(host="node001"))
    assert os.path.isfile(live), "the fixture did not write the file under Luna's hostname"

    # The same node, under the name it used to be written as.
    with open(live, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    with open(older, "w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle)

    # Luna moves it to another group.
    monkeypatch.setattr(backend, "_host_of", lambda node: "node001.cluster")
    monkeypatch.setattr(prometheus_module(), "luna",
                        lambda: SimpleNamespace(node=lambda name: {
                            "hostname": "node001.cluster", "group": "gpu"}))

    answer = backend.repair_hw_rules("node001")

    assert not os.path.isfile(older), "the older file was left beside the live one"
    assert answer["superseded"] == [os.path.basename(older)], answer
    with open(live, encoding="utf-8") as handle:
        rules = yaml.safe_load(handle)
    expressions = [rule["expr"] for group in rules["groups"] for rule in group["rules"]]
    assert all('luna_group="gpu"' in one for one in expressions), \
        f"the live file was never relabelled, so its alerts still match the old group: {expressions}"
    assert answer["rewritten"] == [os.path.basename(live)], \
        f"the relabel happened but was not reported: {answer}"


def _differing_name_backend(tmp_path, monkeypatch):
    """
    A prometheus backend over tmp_path whose one node is keyed node001 in Luna and answers to
    the hostname web-07.cluster, and a reload that is counted.

    The two names do not even share a short form, which is the whole point: nothing about the
    file this node's rules are written under can be read back out of the name it is asked by.
    """
    from trixd.plugins.prometheus import backend as prometheus

    nodes = {"node001": {"hostname": "web-07.cluster", "group": "compute"}}

    class Luna:
        """
        That one node, and nothing under any other name.
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

    def query(url, params=None, **_):
        """
        One component, under the hostname.
        """
        result = [{"metric": {"hostname": "web-07.cluster", "luna_group": "compute",
                              "class": "memory", "description": "DIMM", "product": "P",
                              "vendor": "V", "serial": "S1"}}]
        return SimpleNamespace(status_code=200, text="", json=lambda: {"data": {"result": result}})

    reloads = []

    def reload(argv, **kwargs):
        """
        systemctl reload prometheus-server, counted.
        """
        reloads.append(argv)
        return SimpleNamespace(ok=True, out="", err="")

    monkeypatch.setattr(prometheus, "luna", lambda: Luna())
    monkeypatch.setattr(prometheus.requests, "get", query)
    monkeypatch.setattr(prometheus.shell, "run", reload)
    monkeypatch.setattr(prometheus, "RELOADS", prometheus.SharedReload())
    backend = prometheus.Backend()
    backend.config(body={"rules_directory": str(tmp_path)})
    return backend, reloads


def _kept_copies(directory) -> int:
    """
    How many rule files sit under backup/, retired or copied aside before a write.
    """
    backup = directory / "backup"
    return len(list(backup.iterdir())) if backup.is_dir() else 0


def test_a_node_is_resolved_through_luna_and_not_by_what_its_file_is_called(rig, tmp_path,
                                                                           monkeypatch):
    """
    A node keyed node001 in Luna and answering to web-07.cluster: its rule file is written
    under the hostname, and no name it is asked by can be matched to that file.

    A node is unique in Luna, so Luna can say which file is this node's and a filename cannot.
    Read out of the filename, the file on disk is correct and invisible: the rules are a 404,
    the retire reports nothing and leaves a node that is gone still alerting, and the import a
    node asks for itself after boot regenerates, backs up and reloads Prometheus - on every
    boot of every such node, for ever.
    """
    backend, reloads = _differing_name_backend(tmp_path, monkeypatch)

    first = backend.import_hw_rules("node001", {})
    written = tmp_path / "trix.hw.web-07.cluster.rules"
    assert first["changed"] is True and written.is_file(), \
        f"the first import did not write the file this test is about: {first}"

    # The same node booting again, unchanged: no write, no backup, no reload.
    reloaded, kept = len(reloads), _kept_copies(tmp_path)
    again = backend.import_hw_rules("node001", {})
    assert again["changed"] is False, \
        f"the boot import cannot see the file it wrote, so it writes it again: {again}"
    assert len(reloads) == reloaded, "Prometheus was reloaded for a node that had not changed"
    assert _kept_copies(tmp_path) == kept, "a node that had not changed was backed up"

    assert backend.get_hw_rules("node001").get("groups"), \
        "rules that are on disk and correct are not served to a caller asking by the node"

    retired = backend.remove_hw_rules("node001")
    assert retired["retired"] == ["trix.hw.web-07.cluster.rules"], \
        f"a node that is gone keeps its alerts loading: {retired}"
    assert not written.exists(), "the file reported as retired is still in the glob"


def test_a_relabel_carries_every_node_label_and_not_just_the_hostname():
    """
    Enumerated from NODE_LABELS rather than listed one by one, because the relabel rewrites
    exactly those matchers in the expression: a label it rewrites in the selector and not in
    the labels block is one the file goes on naming wrongly, and it gets no second chance -
    the next pass sees an expression that is already right and reports nothing to do.
    """
    from trixd.plugins.prometheus.backend import NODE_LABELS, _relabel_document

    was = {"hostname": "node001.cluster", "luna_group": "compute"}
    want = {"hostname": "web-07.cluster", "luna_group": "gpu"}
    assert set(NODE_LABELS) == set(want), \
        f"{sorted(set(NODE_LABELS) - set(want))} was added as a node label and needs a value " \
        f"here before this test can say anything about it"
    selector = ",".join(f'{key}="{was[key]}"' for key in NODE_LABELS)
    document = {"groups": [{"name": "trinityx_hw", "rules": [
        {"alert": "MissingHardwareComponent", "expr": f"absent(lshw_device{{{selector}}})",
         "labels": dict(was)}]}]}

    assert _relabel_document(document, want) is True, "the move was not seen at all"

    labels = document["groups"][0]["rules"][0]["labels"]
    stale = {key: labels[key] for key in NODE_LABELS if labels[key] != want[key]}
    assert not stale, f"the selector was rewritten and these labels were left behind: {stale}"
    assert _relabel_document(document, want) is False, \
        "a second pass still finds work, so nothing about this file ever settles"


def _hw_file_labelled(directory, host, group, parts=("memory",)):
    """
    A hardware rule file whose rules also carry the node labels, as Luna's own files do.
    """
    import yaml

    path = _hw_file(directory, host, group, parts)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for rule in document["groups"][0]["rules"]:
        rule["labels"].update({"hostname": host, "luna_group": group})
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_the_reconcile_leaves_no_rule_labelled_with_the_group_the_node_has_left(
        rig, tmp_path, monkeypatch):
    """
    A group move reaches the labels block of every rule and not only its selector.

    These labels are the compatibility surface: dashboards and alert rules group by them. The
    pass that rewrites the selector is also the only one that will ever look - it stamps the
    file as seen, and every pass after it finds an expression that is already right - so a
    label left behind here is left behind for good.
    """
    import yaml

    from trixd.plugins.prometheus.backend import NODE_LABELS

    backend = _prometheus_rig(tmp_path, monkeypatch,
                              {"node001": {"hostname": "node001.cluster", "group": "gpu"}})
    path = _hw_file_labelled(tmp_path, "node001.cluster", "compute")

    first = backend.render_rule_files()

    assert first["repaired"] == ["trix.hw.node001.cluster.rules"], \
        f"the group move was not seen at all, so this says nothing about the labels: {first}"
    labels = yaml.safe_load(path.read_text(encoding="utf-8"))["groups"][0]["rules"][0]["labels"]
    assert labels.get("luna_group") == "gpu", \
        f"the selector was rewritten and the label still names the group it has left: {labels}"
    assert [key for key in NODE_LABELS if key not in labels] == [], \
        f"a node label the file carried was dropped rather than corrected: {labels}"
    assert backend.render_rule_files()["repaired"] == [], \
        "the file never settles, so every reconcile pass rewrites and reloads it"


def test_a_luna_that_cannot_answer_does_not_stop_a_retire(rig, tmp_path, monkeypatch):
    """
    Retiring a node's files needs no Luna: the names on disk are enough, and the delete hook
    runs exactly when Luna may no longer hold the node - or may not answer at all.

    Resolving a node through Luna is what makes the other routes right, and it must not turn
    a route that worked off the file names into a 503: a node deleted while Luna is down
    would keep alerting with nothing left to notice. Luna is unreachable here, which the
    client raises the same way as a refused token or a 5xx.
    """
    from trixd.common.errors import ServiceUnavailable
    from trixd.plugins.prometheus import backend as prometheus

    class Luna:
        """
        Luna, not answering.
        """

        def node(self, name):
            """
            What the client raises when it cannot be asked.
            """
            raise ServiceUnavailable("Luna is not answering")

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    monkeypatch.setattr(prometheus, "luna", lambda: Luna())
    live = _hw_file(tmp_path, "node009.cluster", "compute")

    try:
        answer = backend.remove_hw_rules("node009")
    except ServiceUnavailable as refusal:
        raise AssertionError(
            f"a Luna that cannot answer stopped a retire that needs none of it: "
            f"{refusal.message}") from None

    assert answer["retired"] == ["trix.hw.node009.cluster.rules"], \
        f"the file was found by name and not retired: {answer}"
    assert not live.exists(), "the file reported as retired is still in the glob"


def test_only_the_last_copies_of_one_node_s_rules_are_kept(rig, tmp_path, monkeypatch):
    """
    A copy is taken before every write and none was ever discarded: measured on a four-node
    controller, backup/ held 170 copies of one node's rule file and 73 of another. Per node
    rather than over the directory, because one busy node would otherwise push every other
    node's copies out of a shared cap.

    node10's copies carry node1's name as a prefix. A prune keyed on the node or the host
    name instead of the whole file name takes one node's copies for the other's, and the
    node that loses them is the one nobody was writing to.
    """
    from trixd.plugins.prometheus import backend as prometheus

    keep = getattr(prometheus, "KEEP_COPIES", 10)
    known = {"node1": {"hostname": "node1.cluster", "group": "compute"},
             "node10": {"hostname": "node10.cluster", "group": "compute"}}
    backend = _prometheus_rig(tmp_path, monkeypatch, known)
    _hw_file(tmp_path, "node1.cluster", "compute")
    backup = tmp_path / "backup"
    backup.mkdir()
    planted = [f"20260101-{stamp:06d}" for stamp in range(1, keep + 3)]
    for host, stamps in (("node1.cluster", planted), ("node10.cluster", planted[:3])):
        for stamp in stamps:
            (backup / f"trix.hw.{host}.rules.{stamp}").write_text("", encoding="utf-8")

    backend.set_hw_rules({"nodes": [{"node": "node1", "nhc": False}]})

    def copies(host):
        """
        The stamps of the copies held for one node, oldest first.
        """
        return sorted(path.name.split(".rules.")[1]
                      for path in backup.glob(f"trix.hw.{host}.rules.*"))

    held = copies("node1.cluster")
    assert len(held) == keep, \
        f"{len(held)} copies of one node's rule file are held, not {keep}: {held}"
    # The write took a copy of its own, which is the newest of them; the planted ones it
    # pushed out are the oldest, and the rest are named here rather than counted.
    assert held[:-1] == planted[-(keep - 1):], \
        f"the copies kept are not the newest ones: {held}"
    assert held[-1] not in planted, f"the copy this write took is not among those kept: {held}"
    assert copies("node10.cluster") == planted[:3], \
        "pruning one node's copies discarded those of a node whose name it is a prefix of"


def test_a_scope_that_is_no_scope_cannot_answer_as_the_cluster(rig, tmp_path, monkeypatch):
    """
    Every name that was not a controller, a node or a group resolved as the cluster, so a
    misspelling answered for the whole cluster - another operator's deviations included - and
    previewed as though the scope the caller named had been applied.

    What makes a scope real is a lookup rather than its grammar: a controller's own scope is
    an unprefixed hostname, and the file naming the controllers is not replicated, so a row
    held here for the peer is the only thing that says that scope exists.
    """
    from trixd.utils import declarations

    backend = _prometheus_rig(tmp_path, monkeypatch, {})
    backend.config({"exporters_file": _factory_file(tmp_path, {
        "nodes": {"node": {"port": 14200}},
        "controllers": {"ha": {"port": 14205}},
        "hosts": {}})})
    declarations.write("prometheus", "exporter-scope", "cluster", {"node": {"port": 14999}})
    declarations.write("prometheus", "exporter-scope", "ctrl2.cluster", {"ha": {"port": 14777}})

    assert backend.show_applied("cluster") == {"applied": "cluster",
                                               "exporters": {"node": {"port": 14999}}}, \
        "the answer for a scope that does exist is not the one it was"

    typo = backend.show_applied("clustre")

    assert typo["exporters"] != backend.show_applied("cluster")["exporters"], \
        f"a misspelled scope answers exactly as the cluster does: {typo}"
    assert typo.get("exists") is False, \
        f"nothing in the answer says the scope named is no scope at all: {typo}"
    assert backend.show_applied("ctrl2.cluster")["exporters"] == {"ha": {"port": 14777}}, \
        "a controller named by a row and not by this controller's own exporters file is " \
        "resolved as a node"


def test_a_batch_refused_part_way_through_writes_nothing(rig, tmp_path, monkeypatch):
    """
    The names are read before the first write. Validated inside the loop, a bad name at the
    end of a batch left the entries ahead of it written and the reload after it unrun: rule
    files on disk that Prometheus has never been told about, under an answer saying the batch
    was refused.
    """
    from trixd.plugins.prometheus import backend as prometheus

    known = {f"node00{number}": {"hostname": f"node00{number}.cluster", "group": "compute"}
             for number in range(1, 6)}
    backend = _prometheus_rig(tmp_path, monkeypatch, known)
    reloads = []
    monkeypatch.setattr(prometheus.shell, "run",
                        lambda *a, **k: reloads.append(a) or SimpleNamespace(ok=True, out="",
                                                                            err=""))
    monkeypatch.setattr(prometheus, "RELOADS", prometheus.SharedReload())

    def query(url, params=None, **_):
        """
        Prometheus, answering one part for whichever host is asked about.
        """
        host = params["query"].split('hostname="')[1].split('"')[0]
        result = [{"metric": {"hostname": host, "luna_group": "compute", "class": "memory",
                              "description": "DIMM", "product": "P", "vendor": "V",
                              "serial": "S"}}]
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"result": result}})

    monkeypatch.setattr(prometheus.requests, "get", query)
    taken = [{"node": name, "force": True} for name in sorted(known)]

    with pytest.raises(BadRequest):
        backend.set_hw_rules({"nodes": taken + [{"node": "../../etc/passwd"}]})

    written = sorted(path.name for path in tmp_path.glob("trix.hw.*.rules"))
    assert written == [], f"a refused batch wrote these rule files: {written}"
    assert reloads == [], f"a refused batch reloaded Prometheus {len(reloads)} time(s)"

    # The gate the reload sits behind still holds for a batch that is taken: one reload for
    # the lot, and every file on disk.
    answer = backend.set_hw_rules({"nodes": taken})

    assert [item["ok"] for item in answer["nodes"]] == [True] * len(known), \
        f"a batch of names that are all good was not taken: {answer}"
    assert len(list(tmp_path.glob("trix.hw.*.rules"))) == len(known), \
        "a batch that was taken did not write a rule file for every node"
    assert len(reloads) == 1, f"one batch that changed every node, {len(reloads)} reloads"
