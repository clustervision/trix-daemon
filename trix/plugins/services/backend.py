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

from trix.common import logging as trixlog
from trix.common.errors import BadRequest, NotFound
from trix.utils import declarations, shell

LOGGER = trixlog.get("services")

PLUGIN: str = "services"
SYSTEMCTL: str = "/usr/bin/systemctl"
TIMEOUT: int = 60

ACTIONS: tuple = ("start", "stop", "restart", "reload", "status")

# A name only reaches systemd if it is in this catalogue. An endpoint that took
# an arbitrary unit name would be a way to restart anything on the controller.
DEFAULT_CATALOGUE: dict = {
    "services": {
        "prometheus-server": "prometheus-server",
        "prometheus-alertmanager": "prometheus-alertmanager",
        "slurmctld": "slurmctld",
        "slurmdbd": "slurmdbd",
        "dirsrv": "dirsrv@local",
    },
}


class Backend:
    """
    Named units only, and one verb per call.
    """

    def catalogue(self, body: dict = None) -> dict:
        """
        Which names this daemon will act on, and the unit each one means.
        """
        if body:
            return declarations.write(PLUGIN, "catalogue", "", body)
        return declarations.read(PLUGIN, "catalogue", "", dict(DEFAULT_CATALOGUE))

    def _units(self) -> dict:
        return (self.catalogue() or {}).get("services") or {}

    def _unit(self, name: str) -> str:
        unit = self._units().get(name)
        if not unit:
            raise NotFound(f"{name} is not a service this daemon controls")
        return unit

    def list_services(self) -> dict:
        """
        Every controllable service with its current state.
        """
        answer = {}
        for name in sorted(self._units()):
            answer[name] = self._state(self._units()[name])
        return {"services": answer}

    def status(self, name: str) -> dict:
        """
        Whether one service is running.
        """
        unit = self._unit(name)
        return {"service": name, "unit": unit, **self._state(unit)}

    def act(self, name: str, action: str, caller=None) -> dict:
        """
        Start, stop, restart or reload one service.
        """
        action = str(action).lower()
        if action not in ACTIONS:
            raise BadRequest(f'{action} is not one of {", ".join(ACTIONS)}')
        unit = self._unit(name)
        if action == "status":
            return self.status(name)

        result = shell.run([SYSTEMCTL, action, unit], timeout=TIMEOUT)
        who = getattr(caller, "name", "unknown")
        LOGGER.info("%s asked for %s %s, systemd answered %s", who, action, unit, result.code)
        return {
            "service": name,
            "unit": unit,
            "action": action,
            "ok": result.ok,
            "message": result.err or result.out or f"{action} accepted",
            **self._state(unit),
        }

    def _state(self, unit: str) -> dict:
        """
        What systemd says about a unit right now.
        """
        active = shell.run([SYSTEMCTL, "is-active", unit], timeout=TIMEOUT)
        enabled = shell.run([SYSTEMCTL, "is-enabled", unit], timeout=TIMEOUT)
        return {"active": active.out or "unknown", "enabled": enabled.out or "unknown"}
