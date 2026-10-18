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
Control over the systemd units a site has declared controllable.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import re

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure, BadRequest, NotFound
from trixd.common.settings import current as settings
from trixd.core import tasks
from trixd.utils import declarations, shell

LOGGER = trixlog.get("services")

PLUGIN: str = "services"
# The control command, so a container that has no systemd can point this at a
# facade. Same setting and same argument order as luna.ini [SERVICES] COMMAND.
DEFAULT_COMMAND: str = "/usr/bin/systemctl"
TIMEOUT: int = 60

ACTIONS: tuple = ("start", "stop", "restart", "reload", "status")
# A unit or service name: never a leading dash, so it cannot reach systemctl as an option.
UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]*$")

# What one `systemctl show` is asked for. Both names are read because a record answers
# under the name systemd resolved to, which for an alias is not the one asked for.
PROPERTIES: tuple = ("Id", "Names", "LoadState", "ActiveState", "UnitFileState")
# systemd's own word for a unit it has no file for. UnitFileState is empty for one of those
# and empty for a scope as well, so the load state is what the two are told apart by.
NOT_FOUND: str = "not-found"
UNKNOWN: str = "unknown"


def run_task(action: str, unit: str) -> str:
    """
    What the worker runs for a queued service action.

    Checked here and not in `act` alone because the worker is reachable without it:
    `POST /system/notify` dispatches on an event's first segment, which is this plugin's
    own name, and a catalogue can be rewritten between an action being queued and run.
    """
    if action not in ACTIONS:
        raise BadRequest(f'{action} is not one of {", ".join(ACTIONS)}')
    if unit not in Backend()._units().values():
        raise NotFound(f"{unit} is not a unit this daemon controls")
    result = shell.run([_command(), action, unit], timeout=TIMEOUT)
    if not result.ok:
        raise BackendFailure(result.err or result.out or f"{action} {unit} failed")
    return result.out or f"{action} {unit} accepted"


def _command() -> str:
    """
    What runs a unit action here: systemctl, or whatever the ini names instead.
    """
    return settings().services.command or DEFAULT_COMMAND

# A name only reaches systemd if it is in this catalogue, which a manager keeps. An
# endpoint that took an arbitrary unit name would let an operator restart anything.
DEFAULT_CATALOGUE: dict = {
    "services": {
        "prometheus-server": "prometheus-server",
        "prometheus-alertmanager": "prometheus-alertmanager",
        "slurmctld": "slurmctld",
        "slurmdbd": "slurmdbd",
        "dirsrv": "dirsrv@local",
    },
}


def _records(shown: str) -> dict:
    """
    Each unit's state out of one `systemctl show`, under every name systemd gives it.

    Keyed by name rather than by position because `systemctl is-enabled` over several units
    prints one state line for the whole call when it cannot answer for one of them, which
    reads as another unit's state and says nothing. `show` answers for each unit by name.
    """
    found = {}
    for block in (shown or "").split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if not fields:
            continue
        if fields.get("LoadState") == NOT_FOUND:
            state = {"active": NOT_FOUND, "enabled": NOT_FOUND}
        else:
            state = {"active": fields.get("ActiveState") or UNKNOWN,
                     "enabled": fields.get("UnitFileState") or UNKNOWN}
        for name in [fields.get("Id", ""), *(fields.get("Names") or "").split()]:
            if name:
                found[name] = state
    return found


tasks.register(PLUGIN, run_task)


class Backend:
    """
    Named units only, and one verb per call.
    """

    def catalogue(self, body: dict = None) -> dict:
        """
        Which names this daemon will act on, and the unit each one means.
        """
        if body:
            for name, unit in (body.get("services") or {}).items():
                if not (UNIT_NAME.match(str(name)) and UNIT_NAME.match(str(unit))):
                    raise BadRequest(f"{name}: {unit} is not a systemd unit name")
            return declarations.write(PLUGIN, "catalogue", "", body)
        return declarations.read(PLUGIN, "catalogue", "", dict(DEFAULT_CATALOGUE))

    def _units(self) -> dict:
        """
        Every name this daemon may control, and its unit.
        """
        return (self.catalogue() or {}).get("services") or {}

    def _unit(self, name: str) -> str:
        """
        The unit behind a name, or 404 if it is not in the catalogue.
        """
        unit = self._units().get(name)
        if not unit:
            raise NotFound(f"{name} is not a service this daemon controls")
        return unit

    def list_services(self) -> dict:
        """
        Every controllable service with its current state.
        """
        # Read once. Each call is a database session and a query, and this made one per
        # service on top of the one for the listing, so the cost grew with the catalogue
        # for an answer that is the same throughout the call.
        units = self._units()
        states = self._states(units.values())
        return {"services": {name: states[units[name]] for name in sorted(units)}}

    def status(self, name: str) -> dict:
        """
        Whether one service is running.
        """
        unit = self._unit(name)
        return {"service": name, "unit": unit, **self._state(unit)}

    def act(self, name: str, action: str, caller=None) -> dict:
        """
        Queue one action and answer at once: a slow unit must not hold a worker.
        """
        action = str(action).lower()
        if action not in ACTIONS:
            raise BadRequest(f'{action} is not one of {", ".join(ACTIONS)}')
        unit = self._unit(name)
        if action == "status":
            return self.status(name)

        who = getattr(caller, "name", "unknown")
        identifier = tasks.submit(PLUGIN, action, unit, who)
        LOGGER.info("%s asked for %s %s, queued as %s", who, action, unit, identifier)
        return {"service": name, "unit": unit, "action": action,
                "request_id": identifier, "state": tasks.QUEUED}

    def request(self, request_id: str) -> dict:
        """
        Where one queued action got to. This is Luna's /service/status route.
        """
        found = tasks.status(request_id)
        if found is None:
            raise NotFound(f"no request named {request_id}")
        return found

    def _states(self, units) -> dict:
        """
        What systemd says about several units, in one call however many there are.

        This was two commands per unit, so a catalogue of 200 was 400 subprocesses with a
        minute of timeout each, run one after another on the thread holding the request.
        """
        wanted = list(dict.fromkeys(units))
        if not wanted:
            return {}
        shown = shell.run([_command(), "show", "-p", ",".join(PROPERTIES), *wanted],
                          timeout=TIMEOUT)
        found = _records(shown.out)
        unknown = {"active": UNKNOWN, "enabled": UNKNOWN}
        # A catalogue may name an alias, which systemd answers for under its real name, and
        # a name with no suffix, which systemd answers for as a service.
        return {unit: dict(found.get(unit) or found.get(f"{unit}.service") or unknown)
                for unit in wanted}

    def _state(self, unit: str) -> dict:
        """
        What systemd says about a unit right now.
        """
        return self._states([unit])[unit]
