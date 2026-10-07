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
The role's templates, rendered with its own defaults and the facts the installer
sets, then read the way the daemon and its clients read them. No Ansible needed:
jinja2 is what Ansible renders a template with.
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
from configparser import RawConfigParser

import pytest
import yaml

from trix.common import settings as configuration
from trix.common.constants import DAEMON_RELEASE

jinja2 = pytest.importorskip("jinja2")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROLE = os.path.join(ROOT, "ansible", "roles", "trix-daemon")

# What trinityx-combined's group_vars and the luna2 role set before this role runs.
FACTS: dict = {
    "trix_local": "/trinity/local",
    "trix_ssl": "/trinity/local/etc/ssl",
    "trix_ood": "/trinity/local/ondemand",
    "trix_sync": "/trinity/local/sync",
    "trix_version": "16.1",
    "trix_stream": "testing",
    "trix_domain": "cluster",
    "trix_ctrl_hostname": "controller",
    "ansible_hostname": "controller1",
    "ansible_fqdn": "controller1.cluster",
    "ssl_cert_path": "/etc/ssl/certs",
    "luna_secret_key": "004f2af45d3a4e161a7dd2d17fdae47f",
    "luna_protocol": "https",
    "luna_verify_certificate": False,
    "luna_username": "luna",
    "luna_password": "luna-password",
    "ha": False,
}


def _lookup(kind: str, *terms, **options) -> str:
    """
    Ansible's lookup, for the one use the defaults make: the generated API password.
    """
    assert kind == "password", kind
    return "generated-api-password"


def _environment():
    """
    A strict environment: a variable the role uses and nothing defines is an error.
    """
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True,
                                     loader=jinja2.FileSystemLoader(os.path.join(ROLE, "templates")))
    environment.globals["lookup"] = _lookup
    environment.filters["basename"] = os.path.basename
    return environment


def _variables(**overrides) -> dict:
    """
    The defaults, resolved against the facts and each other as Ansible would.
    """
    with open(os.path.join(ROLE, "defaults", "main.yml"), encoding="utf-8") as handle:
        defaults = yaml.safe_load(handle)
    context = {**FACTS, **defaults, **overrides}
    environment = _environment()
    for _ in range(10):
        changed = False
        for key, value in context.items():
            if isinstance(value, str) and "{{" in value:
                rendered = environment.from_string(value).render(context)
                if rendered != value:
                    context[key] = rendered
                    changed = True
        if not changed:
            break
    leftovers = [key for key, value in context.items() if isinstance(value, str) and "{{" in value]
    assert not leftovers, leftovers
    return context


def _render(template: str, **overrides) -> str:
    """
    One template, as the role writes it.
    """
    return _environment().get_template(template).render(_variables(**overrides))


def _tasks() -> list:
    """
    Every task in every task file, blocks opened up.
    """
    found = []

    def walk(items, source):
        """
        A task, or a block holding more of them.
        """
        for item in items or []:
            found.append((source, item))
            for key in ("block", "rescue", "always"):
                walk(item.get(key), source)

    directory = os.path.join(ROLE, "tasks")
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            walk(yaml.safe_load(handle), name)
    return found


def test_the_daemons_trixd_ini_is_one_the_daemon_loads(tmp_path):
    """
    Rendered with the defaults, read by the daemon's own loader, on a single controller.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text(_render("trixd.ini.j2"), encoding="utf-8")
    loaded = configuration.load(str(ini))

    assert loaded.api.bind == "0.0.0.0:8050"
    assert loaded.api.insecure_bind == "127.0.0.1:8051", "the plain port stays on the loopback"
    assert loaded.api.username == "trix"
    assert loaded.api.password == "generated-api-password"
    assert loaded.tokens.algorithm == "HS256"
    assert loaded.tokens.secret_key == FACTS["luna_secret_key"], "Luna's secret, so tokens cross"
    assert loaded.luna.endpoint == "controller1:7050"
    assert loaded.database.url == "sqlite:////trinity/local/trix/var/trix.db"
    assert loaded.auth.chain == ["local", "pam"]
    assert loaded.replication.enabled is False and loaded.replication.peers == []
    assert loaded.replication.ca_file == "/etc/ssl/certs/cluster-ca.crt"
    assert loaded.plugins.directory == "/trinity/local/trix/daemon/plugins"
    assert loaded.plugins.site_directory == "/trinity/local/trix/plugins"
    assert loaded.plugins.enabled == ["directory", "prometheus", "services", "slurm", "genders"]


def test_an_ha_pair_names_the_other_controller_only(tmp_path):
    """
    Each controller's peer is the other one, by the name its certificate carries.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text(_render("trixd.ini.j2", ha=True,
                           all_ctrl_hostname=["controller1", "controller2"]), encoding="utf-8")
    loaded = configuration.load(str(ini))
    assert loaded.replication.enabled is True
    assert loaded.replication.peers == ["controller2.cluster:8050"]


def test_a_client_trixd_ini_carries_no_secret():
    """
    The five client copies hold who to be and where to go, and nothing that signs.
    """
    parser = RawConfigParser()
    parser.read_string(_render("trixd-client.ini.j2"))
    assert parser.sections() == ["API"]
    assert dict(parser.items("API")) == {
        "username": "trix",
        "password": "generated-api-password",
        "endpoint": "controller.cluster:8050",
        "protocol": "https",
        "verify_certificate": "False",
    }


def test_the_unit_starts_the_daemon_on_its_trixd_ini():
    """
    ExecStart is the shared interpreter, the module, and the daemon's own ini.
    """
    parser = RawConfigParser()
    parser.read_string(_render("trix-daemon.service.j2"))
    assert parser.get("Service", "ExecStart") == (
        "/trinity/local/python/bin/python3 -m trix.server "
        "--config /trinity/local/trix/etc/trixd.ini")
    assert parser.get("Service", "ExecReload") == "/bin/kill -s HUP $MAINPID"


def test_every_trixd_ini_the_role_writes_is_called_trixd_ini():
    """
    Never trix.ini: the name says it belongs to trixd, as luna.ini belongs to Luna.
    """
    destinations = []
    for _, task in _tasks():
        for module in ("template", "ansible.builtin.template"):
            if module in task:
                destinations.append(task[module]["dest"])
    assert len(destinations) == 7, destinations
    assert all(os.path.basename(dest) in ("trixd.ini", "trix-daemon.service")
               for dest in destinations), destinations
    pattern = re.compile(r"(?<![\w-])trix\.ini")
    for folder, _, names in os.walk(os.path.join(ROOT, "ansible")):
        for name in names:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                assert not pattern.search(handle.read()), f"{name} names trix.ini"


def test_every_template_handler_and_included_file_exists():
    """
    A renamed template or handler fails here, not halfway through an install.
    """
    with open(os.path.join(ROLE, "handlers", "main.yml"), encoding="utf-8") as handle:
        handlers = {item["name"] for item in yaml.safe_load(handle)}
    for source, task in _tasks():
        for module in ("template", "ansible.builtin.template"):
            if module in task:
                assert os.path.isfile(os.path.join(ROLE, "templates", task[module]["src"])), source
        notified = task.get("notify")
        for name in [notified] if isinstance(notified, str) else notified or []:
            assert name in handlers, f"{source}: notify {name} has no handler"
        included = task.get("include_tasks") or task.get("ansible.builtin.include_tasks")
        if included:
            assert os.path.isfile(os.path.join(ROLE, "tasks", included)), source


def test_the_role_installs_the_link_the_pipeline_publishes():
    """
    The pipeline links <name>-<major.minor>-py3-none-any.whl to the newest build.
    """
    assert _variables()["trixd_pip"] == f"trix_daemon-{DAEMON_RELEASE}-py3-none-any.whl"
