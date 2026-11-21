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
Silences, the alerts they quieten, and what Alertmanager says about itself.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3

from trixd.common import logging as trixlog
from trixd.common.constants import PLUGIN_STATE_LOADED
from trixd.common.errors import BackendFailure, BadRequest, NotFound
from trixd.common.settings import seconds
from trixd.core.plugins.registry import current as registry
from trixd.utils import declarations, tls

LOGGER = trixlog.get("alertmanager")

urllib3.disable_warnings()

PLUGIN: str = "alertmanager"

DEFAULT_CONFIG: dict = {
    "alertmanager_url": "https://localhost:9093",
    # The password only: the installer writes it as root, and the daemon runs as root. It is
    # a path rather than a value so the secret stays out of the declarations the pair
    # replicates to each other, and the account comes from its name.
    "credentials_file": "/etc/trinity/passwords/prometheus/admin.txt",
}

# Alertmanager's own API, which this plugin translates to and from. Its routes are not ours.
API: str = "/api/v2"

# How long an unverified loopback query stays unverified before TLS is tried again. The hour
# is the daemon's cadence for a fallback pass, and what a reissued certificate waits for.
UNVERIFIED_FOR: int = 3600
TIMEOUT: int = 20

# How long a silence lasts when the caller does not say.
DEFAULT_DURATION: str = "2h"

# A silence identifier, which ends up in the path of a request this plugin builds. Nothing
# else passes, so no caller-supplied text can steer where that request goes.
SILENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")

# What a silence can be in, as Alertmanager reports it.
STATES: tuple = ("active", "pending", "expired")

# The plugin the alerts are read from while Alertmanager is away. It is asked for by name
# rather than imported, so the one backend its own routes answer from is the one asked -
# and an operator who unplugged it has unplugged this too.
SOURCE: str = "prometheus"

# How often the degraded answer is said out loud. A console polls this route, so the
# warning is per spell of Alertmanager being away rather than per request.
SAY_DEGRADED_EVERY: int = 3600

# What the caller is told when the alerts are Prometheus'. Prometheus knows nothing of
# silences, so the one thing it cannot say is the one a caller would otherwise read into it.
DEGRADED: str = (
    "Alertmanager did not answer, so these are the alerts Prometheus reports: its own "
    "states, firing and pending, and nothing about what is being quietened. An alert a "
    "silence is holding down is here like any other, and silenced_by and inhibited_by are "
    "absent rather than empty.")


class Unreachable(BackendFailure):
    """
    Alertmanager did not answer at all, as against answering a refusal.
    """


class Backend:
    """
    Alertmanager's silences, alerts and status, in this daemon's own words.
    """

    def __init__(self) -> None:
        """
        Hold what the first call learned about where Alertmanager answers.
        """
        # Where Alertmanager really answered, when that differs from the configured scheme.
        self.alertmanager_url: str = ""
        # When the loopback last failed to verify, so that it is asked unverified until the
        # hour is out rather than per call. It says nothing about any other address.
        self.unverified_from: float = 0.0
        # When the alerts were last answered from Prometheus, so a spell of Alertmanager
        # being away is said once rather than on every poll.
        self.said_degraded: float = 0.0

    # ---------------------------------------------------------------- config

    def config(self, body: dict = None) -> dict:
        """
        Where this plugin reaches Alertmanager, and what it signs in with.
        """
        if body:
            return declarations.merge(PLUGIN, "config", "", body, DEFAULT_CONFIG)
        return declarations.read(PLUGIN, "config", "", DEFAULT_CONFIG)

    def _config(self) -> dict:
        """
        The stored configuration, with the shipped defaults under it.
        """
        return declarations.read(PLUGIN, "config", "", DEFAULT_CONFIG)

    # ---------------------------------------------------------------- silences

    def silences(self, state: str = "") -> dict:
        """
        The silences Alertmanager holds, newest first, optionally one state of them.
        """
        wanted = (state or "").strip().lower()
        if wanted and wanted not in STATES:
            raise BadRequest(f"{state} is not one of {', '.join(STATES)}")
        held = self._call("GET", "/silences").json() or []
        listed = [_silence(one) for one in held
                  if not wanted or (one.get("status") or {}).get("state") == wanted]
        listed.sort(key=lambda one: one["starts_at"], reverse=True)
        return {"count": len(listed), "silences": listed}

    def silence(self, body: dict, caller=None) -> dict:
        """
        Quieten whatever the matchers select, until the duration is out.
        """
        matchers = _matchers(body.get("matchers"))
        comment = str(body.get("comment") or "").strip()
        if not comment:
            raise BadRequest("a silence says why it was made: send comment")
        starts = datetime.now(timezone.utc)
        ends = _ends_at(body, starts)
        answer = self._call("POST", "/silences", {
            "matchers": matchers, "startsAt": _stamp(starts), "endsAt": _stamp(ends),
            "createdBy": caller.name, "comment": comment})
        identifier = str((answer.json() or {}).get("silenceID") or "")
        LOGGER.info("%s silenced %s until %s: %s", caller.name,
                    _selection(matchers), _stamp(ends), comment)
        return {"silence_id": identifier, "created_by": caller.name,
                "starts_at": _stamp(starts), "ends_at": _stamp(ends), "comment": comment,
                "matchers": _ours(matchers)}

    def expire(self, body: dict, caller=None) -> dict:
        """
        End a silence now. Alertmanager keeps the record and marks it expired.
        """
        identifier = str(body.get("silence_id") or "").strip()
        if not SILENCE_ID.match(identifier):
            raise BadRequest("silence_id is the identifier a silence was created with, "
                             "letters, digits and dashes only")
        self._call("DELETE", f"/silence/{identifier}",
                   missing=f"Alertmanager holds no silence {identifier}")
        LOGGER.info("%s expired silence %s", caller.name, identifier)
        return {"silence_id": identifier, "expired_by": caller.name}

    # ---------------------------------------------------------------- alerts

    def alerts(self) -> dict:
        """
        What is firing now, with the silenced and inhibited ones marked as such.

        Prometheus answers this while Alertmanager is away, and `source` says which of the
        two it was. An Alertmanager that answered a refusal is reached, so that refusal
        stands: a credential the account cannot use would otherwise read as a degraded
        answer for ever.
        """
        try:
            held = self._call("GET", "/alerts").json() or []
        except Unreachable as away:
            return self._reported_alerts(away)
        return {"source": PLUGIN, "count": len(held),
                "alerts": [_alert(one) for one in held]}

    def _reported_alerts(self, away: Unreachable) -> dict:
        """
        The alerts Prometheus reports, through the plugin that owns that read.
        """
        entry = registry().get(SOURCE)
        backend = entry.backend if entry and entry.state == PLUGIN_STATE_LOADED else None
        if backend is None:
            raise away
        try:
            answer = backend.reported_alerts()
        except BackendFailure as error:
            raise BackendFailure(f"{away.message}; nor is Prometheus, which answers these "
                                 f"while Alertmanager is away: {error}") from None
        if time.monotonic() - self.said_degraded > SAY_DEGRADED_EVERY:
            self.said_degraded = time.monotonic()
            LOGGER.warning("answering the alerts from Prometheus: %s", away.message)
        held = answer.get("alerts") or []
        return {"source": SOURCE, "degraded": DEGRADED, "count": len(held), "alerts": held}

    def status(self) -> dict:
        """
        Which Alertmanager answered, and whether it has found the other one.
        """
        document = self._call("GET", "/status").json() or {}
        cluster = document.get("cluster") or {}
        return {"url": self.alertmanager_url
                       or self._config().get("alertmanager_url",
                                             DEFAULT_CONFIG["alertmanager_url"]),
                "version": (document.get("versionInfo") or {}).get("version", ""),
                "uptime": document.get("uptime", ""),
                "cluster": {"status": cluster.get("status", ""),
                            "name": cluster.get("name", ""),
                            "peers": [str(peer.get("address") or "")
                                      for peer in cluster.get("peers") or []]}}

    # ---------------------------------------------------------------- reaching it

    def _credentials(self) -> tuple:
        """
        The account Alertmanager is asked as, its password, and the file both came from.

        The file is named for the account it belongs to, because the installer builds these
        paths from the username - so a file named for anything else, a salt or a passphrase,
        is not a credential this can present. Read on every call, so a rotated password is
        used without a restart.
        """
        path = self._config().get("credentials_file") or DEFAULT_CONFIG["credentials_file"]
        try:
            with open(path, encoding="utf-8") as handle:
                password = handle.read().strip()
        except OSError as error:
            raise BackendFailure("the Alertmanager credential could not be read from "
                                 f"{path}: {error}") from None
        if not password:
            raise BackendFailure(f"the Alertmanager credential at {path} is empty")
        return (os.path.splitext(os.path.basename(path))[0], password, path)

    def _call(self, method: str, path: str, body: dict = None, missing: str = ""):
        """
        One request to Alertmanager. Its certificate is verified against the system trust
        store, which is where the cluster CA is installed; the first call is also the probe
        that finds a loopback address the certificate does not carry.
        """
        configured = self._config().get("alertmanager_url",
                                        DEFAULT_CONFIG["alertmanager_url"])
        url = self.alertmanager_url or configured
        account, password, secret = self._credentials()
        upgraded = False
        for _ in (1, 2, 3):
            verify = not (tls.is_loopback(url) and self.unverified_from
                          and time.monotonic() - self.unverified_from <= UNVERIFIED_FOR)
            try:
                # The store is asked for inside the try, so a system without one is a
                # verification that cannot succeed and is answered where a failed one is -
                # never by asking anyway.
                answer = requests.request(method, f"{url}{API}{path}", json=body,
                                          auth=(account, password), timeout=TIMEOUT,
                                          verify=tls.system_trust() if verify else False)
            except requests.exceptions.SSLError as error:
                # The controller's certificate carries the box's own names, never localhost,
                # so the loopback is the one address a healthy cluster cannot verify. Any
                # other address is somebody else's, and a refusal there stays a refusal.
                if verify and tls.is_loopback(url):
                    self.unverified_from = time.monotonic()
                    LOGGER.warning("Alertmanager at %s did not verify (%s); asking the "
                                   "loopback unverified, and verifying again in %ss",
                                   url, error, UNVERIFIED_FOR)
                    continue
                raise Unreachable(f"Alertmanager is not answering: {error}") from None
            except requests.RequestException as error:
                raise Unreachable(f"Alertmanager is not answering: {error}") from None
            if answer.status_code == 400 and "HTTPS server" in answer.text and not upgraded:
                url = tls.other_scheme(url)
                upgraded = True
                continue
            break
        if missing and answer.status_code == 404:
            raise NotFound(missing)
        if answer.status_code == 401:
            raise BackendFailure(f"Alertmanager at {url} refused the account {account}, "
                                 f"whose password was read from {secret}")
        if answer.status_code >= 400:
            raise BackendFailure(f"Alertmanager answered {answer.status_code} at {url}: "
                                 f"{answer.text.strip()[:200]}")
        if url != (self.alertmanager_url or configured):
            LOGGER.warning("Alertmanager answers on %s, not %s; using that from now on",
                           url, configured)
            self.alertmanager_url = url
        return answer


def _matchers(declared) -> list:
    """
    Our matchers in Alertmanager's words: `label` is its `name`, and a negative matcher is
    its `isEqual: false`.
    """
    if not isinstance(declared, list) or not declared:
        raise BadRequest("a silence needs at least one matcher saying what it quietens")
    built = []
    for one in declared:
        if not isinstance(one, dict):
            raise BadRequest("every matcher is a label, a value, and optionally regex "
                             "and negative")
        label = str(one.get("label") or "").strip()
        if not label:
            raise BadRequest("every matcher names the label it matches on")
        if one.get("value") is None:
            raise BadRequest(f"the matcher on {label} says no value to match")
        built.append({"name": label, "value": str(one["value"]),
                      "isRegex": bool(one.get("regex", False)),
                      "isEqual": not bool(one.get("negative", False))})
    return built


def _ours(matchers: list) -> list:
    """
    The same matchers back in our own words, so a caller reads what it sent.
    """
    return [{"label": one["name"], "value": one["value"], "regex": one["isRegex"],
             "negative": not one["isEqual"]} for one in matchers]


def _selection(matchers: list) -> str:
    """
    What a silence quietens, as one line for the log.
    """
    return ",".join(f"{one['name']}{'!' if not one['isEqual'] else ''}"
                    f"{'~' if one['isRegex'] else '='}{one['value']}" for one in matchers)


def _ends_at(body: dict, now: datetime) -> datetime:
    """
    When the silence is over: the time given, or the duration from now.
    """
    given = str(body.get("ends_at") or "").strip()
    if given:
        try:
            ends = datetime.fromisoformat(given.replace("Z", "+00:00"))
        except ValueError:
            raise BadRequest(f"{given} is not a time such as 2026-11-19T18:00:00Z") from None
        ends = ends if ends.tzinfo else ends.replace(tzinfo=timezone.utc)
    else:
        try:
            length = seconds(body.get("duration") or DEFAULT_DURATION, fallback=0)
        except ValueError as error:
            raise BadRequest(str(error)) from None
        ends = now + timedelta(seconds=length)
    if ends <= now:
        raise BadRequest("a silence ends after it starts; send a duration such as 2h, or "
                         "an ends_at in the future")
    return ends


def _stamp(when: datetime) -> str:
    """
    A time as Alertmanager writes and reads it.
    """
    return when.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _silence(held: dict) -> dict:
    """
    One silence as this daemon describes it, rather than as Alertmanager returns it.
    """
    status = held.get("status") or {}
    return {"silence_id": str(held.get("id") or ""),
            "state": status.get("state", ""),
            "created_by": held.get("createdBy", ""),
            "comment": held.get("comment", ""),
            "starts_at": held.get("startsAt", ""),
            "ends_at": held.get("endsAt", ""),
            "matchers": [{"label": one.get("name", ""), "value": one.get("value", ""),
                          "regex": bool(one.get("isRegex", False)),
                          "negative": not bool(one.get("isEqual", True))}
                         for one in held.get("matchers") or []]}


def _alert(held: dict) -> dict:
    """
    One alert, with the labels that name it and what is keeping it quiet.
    """
    status = held.get("status") or {}
    labels = held.get("labels") or {}
    return {"name": labels.get("alertname", ""),
            "state": status.get("state", ""),
            "labels": labels,
            "annotations": held.get("annotations") or {},
            "starts_at": held.get("startsAt", ""),
            "silenced_by": list(status.get("silencedBy") or []),
            "inhibited_by": list(status.get("inhibitedBy") or [])}
