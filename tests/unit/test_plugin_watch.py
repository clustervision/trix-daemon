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
[PLUGINS] AUTO_RELOAD: a custom plugin whose files changed is loaded again without a
restart, and nothing else is.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import logging
import os

import pytest

from trixd.common import settings as configuration
from trixd.common.constants import API_PREFIX
from trixd.common.settings import current as settings
from trixd.core import lifespan
from trixd.core.plugins import loader, scaffold, watch
from trixd.core.plugins.registry import current as registry

from tests.conftest import build

HELLO: str = f"{API_PREFIX}/hello"
FIRST: str = '"plugin": PLUGIN'
SECOND: str = '"plugin": "the second version"'


def _backend(name: str = "hello") -> str:
    """
    A scaffolded custom plugin's backend file, written if it is not there yet.
    """
    custom = settings().plugins.custom_directory
    if not os.path.isdir(os.path.join(custom, name)):
        scaffold.new(name, custom)
    return os.path.join(custom, name, "backend.py")


def _edit(path: str, old: str, new: str) -> None:
    """
    Change a file the way an editor does. Every edit here changes its length too, so no
    test leans on how fine the filesystem's clock is.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert old in text and len(old) != len(new)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new))


@pytest.fixture()
def loads(monkeypatch) -> list:
    """
    Every name handed to the loader while the test runs, the loader still doing its work.
    """
    asked = []
    real = loader.load

    def load(app, name, path=""):
        asked.append(name)
        return real(app, name, path)
    monkeypatch.setattr(loader, "load", load)
    return asked


def test_a_changed_plugin_answers_with_its_new_code_and_no_restart(rig, client, headers):
    """
    The edit is seen at one look and loaded at the next, once the files stood still
    between the two: a plugin caught halfway through a save is not loaded.
    """
    backend = _backend()
    loader.load(rig, "hello")
    watching = watch.Watch()
    watching.baseline(rig)
    assert client.get(HELLO, headers=headers).json()["plugin"] == "hello"

    _edit(backend, FIRST, SECOND)
    assert watching.once() == [], "loaded at the look that first saw the change"
    assert client.get(HELLO, headers=headers).json()["plugin"] == "hello"
    assert watching.once() == ["hello"]
    assert client.get(HELLO, headers=headers).json()["plugin"] == "the second version"
    # What the load itself wrote beside the sources is not a change.
    assert watching.once() == [] and watching.once() == []


def test_a_file_still_changing_is_waited_for(rig, loads):
    """
    Two different states at two looks is a save in progress; the load waits for two alike.
    """
    backend = _backend()
    loader.load(rig, "hello")
    watching = watch.Watch()
    watching.baseline(rig)
    del loads[:]

    _edit(backend, FIRST, '"plugin": "halfway"')
    assert watching.once() == []
    _edit(backend, '"plugin": "halfway"', SECOND)
    assert watching.once() == [] and not loads
    assert watching.once() == ["hello"]


def test_only_sources_count_as_a_change(rig):
    """
    The fingerprint is the .py and .yaml files: compiled files and anything else an
    editor or the interpreter leaves in the directory never cause a load.
    """
    plugin = os.path.dirname(_backend())
    before = watch.fingerprint(plugin)
    assert [name for name, _, _ in before] == ["__init__.py", "backend.py", "manifest.yaml"]

    os.makedirs(os.path.join(plugin, "__pycache__"))
    for name in (os.path.join("__pycache__", "backend.py"), "README", "backend.py.swp"):
        with open(os.path.join(plugin, name), "w", encoding="utf-8") as handle:
            handle.write("not a source")
    assert watch.fingerprint(plugin) == before

    _edit(os.path.join(plugin, "manifest.yaml"), "One or two lines", "A line")
    assert watch.fingerprint(plugin) != before


def test_a_broken_edit_is_refused_and_the_old_code_keeps_serving(rig, client, headers):
    """
    A syntax error saved under a running daemon costs nothing: the route answers as before
    and the plugin list carries the reason.
    """
    backend = _backend()
    loader.load(rig, "hello")
    watching = watch.Watch()
    watching.baseline(rig)

    _edit(backend, "class Backend:", "class Backend(:")
    assert watching.once() == [] and watching.once() == []
    answer = client.get(HELLO, headers=headers)
    assert answer.status_code == 200 and answer.json()["plugin"] == "hello"
    entry = registry().get("hello")
    assert entry.state == "loaded" and entry.reason, "the refusal was not recorded"


def test_a_broken_plugin_is_tried_once_and_again_when_it_changes(rig, client, headers, loads):
    """
    A refusal is logged at ERROR by the loader, so one attempt per state of the files: a
    plugin left broken must not be imported and refused at every look.
    """
    backend = _backend()
    loader.load(rig, "hello")
    watching = watch.Watch()
    watching.baseline(rig)
    del loads[:]

    _edit(backend, "class Backend:", "class Backend(:")
    for _ in range(5):
        assert watching.once() == []
    assert loads == ["hello"]

    _edit(backend, "class Backend(:", "class Backend:")
    _edit(backend, FIRST, SECOND)
    assert watching.once() == [] and watching.once() == ["hello"]
    assert loads == ["hello", "hello"]
    assert client.get(HELLO, headers=headers).json()["plugin"] == "the second version"


def test_a_look_that_finds_nothing_changed_does_nothing(rig, monkeypatch):
    """
    No change, no reload: no load, no discovery and no read of the database, with a
    directory of plugins and with none at all.
    """
    def never(*args, **kwargs):
        raise AssertionError("an unchanged look reached the loader")

    watching = watch.Watch()
    assert not os.path.isdir(settings().plugins.custom_directory)
    watching.baseline(rig)
    monkeypatch.setattr(loader, "load", never)
    monkeypatch.setattr(loader, "discover", never)
    monkeypatch.setattr(loader, "_disabled_names", never)
    assert watching.once() == [] and watching.once() == []
    monkeypatch.undo()

    _backend()
    loader.load(rig, "hello")
    watching.baseline(rig)
    assert watching.seen["hello"][1], "nothing was fingerprinted, so nothing could change"
    monkeypatch.setattr(loader, "load", never)
    monkeypatch.setattr(loader, "discover", never)
    monkeypatch.setattr(loader, "_disabled_names", never)
    for _ in range(3):
        assert watching.once() == []


def test_an_unplugged_plugin_is_never_plugged_back_in(rig, client, headers, loads):
    """
    Unplugging is a decision somebody made. Editing the files afterwards does not undo
    it, whether this process did the unplugging or only the record of it is left.
    """
    backend = _backend()
    loader.load(rig, "hello")
    loader.unload(rig, "hello")
    settings().plugins.enabled.append("hello")
    watching = watch.Watch()
    watching.baseline(rig)
    del loads[:]

    _edit(backend, FIRST, SECOND)
    assert watching.once() == [] and watching.once() == []
    assert registry().get("hello").state == "disabled"

    # As after a restart that found it on disk only later: recorded, not in the registry.
    registry().plugins.pop("hello")
    _edit(backend, SECOND, '"plugin": "the third"')
    assert watching.once() == [] and watching.once() == []
    assert not loads and registry().get("hello") is None
    assert client.get(HELLO, headers=headers).status_code == 404


def test_a_new_plugin_is_plugged_in_only_where_a_restart_would_load_it(rig, client, headers):
    """
    ENABLED stays the gate: a name it does not list is left on disk, one it lists is
    plugged in, and so is any name when it lists nothing.
    """
    watching = watch.Watch()
    watching.baseline(rig)
    assert "hello" not in settings().plugins.enabled and settings().plugins.enabled

    backend = _backend()
    assert watching.once() == [] and watching.once() == []
    assert registry().get("hello") is None
    assert client.get(HELLO, headers=headers).status_code == 404

    settings().plugins.enabled.append("hello")
    _edit(backend, FIRST, SECOND)
    assert watching.once() == [] and watching.once() == ["hello"]
    assert client.get(HELLO, headers=headers).json()["plugin"] == "the second version"

    settings().plugins.enabled = []
    _backend("other")
    assert watching.once() == [] and watching.once() == ["other"]
    assert client.get(f"{API_PREFIX}/other", headers=headers).status_code == 200


def test_a_plugin_plugged_in_by_hand_is_reloaded(rig, client, headers):
    """
    One that is serving is reloaded whatever ENABLED says: it was plugged in on purpose.
    """
    backend = _backend()
    loader.load(rig, "hello")
    assert "hello" not in settings().plugins.enabled
    watching = watch.Watch()
    watching.baseline(rig)

    _edit(backend, FIRST, SECOND)
    assert watching.once() == [] and watching.once() == ["hello"]
    assert client.get(HELLO, headers=headers).json()["plugin"] == "the second version"


def test_a_custom_directory_under_a_shipped_name_reloads_nothing(rig, loads):
    """
    A custom plugin cannot take a shipped plugin's name, so a change under that name in the
    custom directory is not a reason to load the shipped one again.
    """
    backend = _backend("services")
    assert registry().get("services").state == "loaded"
    watching = watch.Watch()
    watching.baseline(rig)
    del loads[:]

    _edit(backend, FIRST, SECOND)
    assert watching.once() == [] and watching.once() == []
    assert not loads


def test_a_look_that_fails_is_said_once_and_the_next_one_still_runs(monkeypatch):
    """
    Nothing restarts the thread, so a look that raises must not end it, and one that keeps
    failing the same way must not write a line every few seconds.
    """
    said = []

    class Said(logging.Handler):
        def emit(self, record):
            said.append(record.getMessage())

    watching = watch.Watch()
    looks = []

    def unreadable() -> list:
        looks.append(1)
        if len(looks) == 3:
            watching.stop.set()
        raise OSError("the custom directory cannot be read")

    handler = Said()
    watch.LOGGER.addHandler(handler)
    monkeypatch.setattr(watch, "INTERVAL", 0)
    monkeypatch.setattr(watching, "once", unreadable)
    try:
        watching._loop()
    finally:
        watch.LOGGER.removeHandler(handler)
    assert len(looks) == 3, "the first failed look ended the thread"
    assert len([line for line in said if "cannot be read" in line]) == 1, said


def test_the_setting_is_off_unless_the_ini_says_yes(tmp_path):
    """
    Off by default, and read the way every other switch in trixd.ini is.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[PLUGINS]\nENABLED = services\n", encoding="utf-8")
    assert configuration.load(str(ini)).plugins.auto_reload is False
    ini.write_text("[PLUGINS]\nENABLED = services\nAUTO_RELOAD = yes\n", encoding="utf-8")
    assert configuration.load(str(ini)).plugins.auto_reload is True


def test_no_thread_is_started_unless_it_is_asked_for(rig):
    """
    A daemon that came up on the defaults has no watch running, and starting one by hand
    is refused the same way.
    """
    running = watch.current().thread
    assert running is None or not running.is_alive(), "the daemon started a watch unasked"
    watching = watch.Watch()
    assert watching.start(rig) is False
    assert watching.thread is None


def test_the_daemon_starts_the_watch_where_asked_and_stops_it(tmp_path, monkeypatch):
    """
    With AUTO_RELOAD on the startup sequence starts the thread, and shutdown ends it.
    """
    read = configuration.load

    def asked_for(path: str):
        config = read(path)
        config.plugins.auto_reload = True
        return config
    monkeypatch.setattr(configuration, "load", asked_for)
    app = build(tmp_path, monkeypatch=monkeypatch)
    running = watch.current().thread
    try:
        assert running is not None and running.is_alive()
        assert watch.current().app is app
    finally:
        lifespan.stop(app)
        if running is not None:
            running.join(5)
        watch.current().thread = None
    assert not running.is_alive(), "shutdown left the watch running"
