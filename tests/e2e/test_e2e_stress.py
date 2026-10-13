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
Load: sustained reads, and the boot storm, when a cluster boots and every node asks for a
token and its hardware rules at once. Each prints what it measured. Run with -m stress;
TRIXD_STRESS_NODES, TRIXD_STRESS_THREADS and TRIXD_STRESS_SECONDS scale it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from tests.e2e.stack import MARK

NODES = int(os.environ.get("TRIXD_STRESS_NODES", "1000"))
THREADS = int(os.environ.get("TRIXD_STRESS_THREADS", "32"))
SECONDS = float(os.environ.get("TRIXD_STRESS_SECONDS", "20"))


def _percentile(values: list, share: float) -> float:
    """
    The value below which this share of the sorted values falls.
    """
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share))] if ordered else 0.0


def _report(title: str, timings: list, errors: dict, elapsed: float) -> str:
    """
    One line of what was measured.
    """
    line = (f"{title}: {len(timings)} requests in {elapsed:.1f}s = {len(timings) / elapsed:.0f}/s, "
            f"p50 {_percentile(timings, 0.5) * 1000:.0f} ms, p95 {_percentile(timings, 0.95) * 1000:.0f} ms, "
            f"p99 {_percentile(timings, 0.99) * 1000:.0f} ms, errors {errors or 'none'}")
    print(line, file=sys.stderr)
    return line


@pytest.mark.stress
@pytest.mark.parametrize("path,role", [("/system/health", None), ("/system/plugins", "reader"),
                                       ("/prometheus/rules", "reader")])
def test_sustained_reads(stack, path, role):
    """
    THREADS callers for SECONDS: no error, and p95 within two seconds.
    """
    token = stack.as_role(role) if role else None
    timings, errors, lock = [], {}, threading.Lock()
    stop = time.monotonic() + SECONDS

    def caller() -> None:
        """
        One client, its own connection, as fast as it can.
        """
        session = requests.Session()
        headers = {"x-access-tokens": token} if token else {}
        while time.monotonic() < stop:
            started = time.monotonic()
            try:
                status = session.get(f"{stack.base}{path}", headers=headers, timeout=30,
                                     verify=False).status_code
            except requests.RequestException as error:
                status = type(error).__name__
            with lock:
                timings.append(time.monotonic() - started)
                if status != 200:
                    errors[status] = errors.get(status, 0) + 1

    began = time.monotonic()
    threads = [threading.Thread(target=caller) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _report(f"GET {path}", timings, errors, time.monotonic() - began)
    assert not errors, errors
    assert _percentile(timings, 0.95) < 2.0


@pytest.mark.stress
def test_a_boot_storm_of_nodes(stack):
    """
    NODES nodes each sign in and ask for their hardware rules, THREADS at a time. Locally the
    stand-in Luna holds them all and Prometheus reloads are counted: the first wave writes
    every file but reloads far fewer times, and a second wave, nothing changed, reloads none.
    On a controller the nodes Luna holds are asked in turn.
    """
    counter = None
    if stack.local:
        with stack.luna.lock:
            for number in range(1, NODES + 1):
                name = f"{MARK}n{number:05d}"
                stack.luna.nodes[name] = {"name": name, "hostname": f"{name}.cluster",
                                          "group": "compute", "interfaces": []}
        counter = os.path.join(stack.scratch, "reloads.txt")
        script = os.path.join(stack.scratch, "count_reload.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(f"open({counter!r}, 'a').write('x')\n")
        assert stack.call("PATCH", "/prometheus/config", stack.rootus(),
                          json={"reload_command": f"{sys.executable} {script}"}).status_code == 200
        names = [f"{MARK}n{number:05d}" for number in range(1, NODES + 1)]
    else:
        names = [stack.nodes[n % len(stack.nodes)] for n in range(min(NODES, 50))] if stack.nodes else []
    if not names:
        pytest.skip("Luna holds no node")

    def boot(name: str):
        """
        What one node's hook does after boot: a token, then its rules.
        """
        session = requests.Session()
        started = time.monotonic()
        token = session.post(f"{stack.base}/auth/node/{name}", timeout=60, verify=False,
                             json={"username": stack.user, "password": stack.password})
        if token.status_code != 201:
            return time.monotonic() - started, f"token {token.status_code}", None
        answer = session.patch(f"{stack.base}/prometheus/hw-rules/{name}", timeout=120,
                               verify=False, json={"force": False},
                               headers={"x-access-tokens": token.json()["token"]})
        changed = answer.json().get("changed") if answer.status_code == 200 else None
        return time.monotonic() - started, answer.status_code, changed

    def wave(title: str) -> list:
        """
        Every node once, THREADS at a time.
        """
        began = time.monotonic()
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            results = list(pool.map(boot, names))
        errors = {}
        for _, status, _ in results:
            if status != 200:
                errors[status] = errors.get(status, 0) + 1
        _report(title, [item[0] for item in results], errors, time.monotonic() - began)
        if stack.local:
            assert not errors, errors
        return results

    def reloads() -> int:
        """
        How many times Prometheus was reloaded so far.
        """
        if counter is None or not os.path.isfile(counter):
            return 0
        with open(counter, encoding="utf-8") as handle:
            return len(handle.read())

    first = wave(f"boot storm, {len(names)} nodes, first boot")
    after_first = reloads()
    second = wave(f"boot storm, {len(names)} nodes, reboot with nothing changed")
    after_second = reloads()
    print(f"Prometheus reloads: first wave {after_first}, second wave "
          f"{after_second - after_first}", file=sys.stderr)
    if stack.local:
        assert sum(1 for item in first if item[2]) == len(names), "every first boot writes"
        assert after_first < len(names) / 2, f"{after_first} reloads for {len(names)} nodes"
        assert all(item[2] is False for item in second), "an unchanged node is no change"
        assert after_second == after_first, "no reload when nothing changed"
