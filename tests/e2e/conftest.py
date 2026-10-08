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
The end-to-end suite runs only when asked: TRIXD_E2E=local starts a daemon here,
TRIXD_E2E_URL names one already running. Without either it is skipped, so the unit run
never waits on a daemon. Stress tests also need -m stress.
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

from tests.e2e.stack import Stack, operations

WANTED = bool(os.environ.get("TRIXD_E2E") or os.environ.get("TRIXD_E2E_URL"))


def pytest_configure(config) -> None:
    """
    Name the stress marker, so -m stress selects the load tests.
    """
    config.addinivalue_line("markers", "stress: sustained load and boot storms; slow")


def pytest_collection_modifyitems(config, items) -> None:
    """
    Skip the whole suite unless a daemon was asked for, and the load tests unless -m stress.
    """
    marked = config.getoption("-m") or ""
    for item in items:
        if "tests/e2e" not in item.nodeid.replace(os.sep, "/"):
            continue
        if not WANTED:
            item.add_marker(pytest.mark.skip(reason="set TRIXD_E2E=local or TRIXD_E2E_URL"))
        elif "stress" in item.keywords and "stress" not in marked:
            item.add_marker(pytest.mark.skip(reason="load test: run with -m stress"))


@pytest.fixture(scope="session")
def stack():
    """
    One daemon for the whole run, and everything it needs torn down afterwards.
    """
    if not WANTED:
        pytest.skip("set TRIXD_E2E=local or TRIXD_E2E_URL")
    running = Stack().start()
    yield running
    running.stop()


@pytest.fixture(scope="session")
def document(stack):
    """
    The live OpenAPI document, read once.
    """
    return stack.document()


def pytest_generate_tests(metafunc) -> None:
    """
    Parametrise over the published operations without a daemon: the committed document
    has the same routes the live one does, and a unit test keeps them in step.
    """
    if "operation" in metafunc.fixturenames:
        import json

        from tests.e2e.stack import ROOT

        with open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8") as handle:
            published = operations(json.load(handle))
        wanted = getattr(metafunc.function, "access", None)
        chosen = [item for item in published
                  if wanted is None or item[2].get("x-trixd-access") in wanted]
        metafunc.parametrize("operation", chosen,
                             ids=[f"{verb} {path}" for verb, path, _ in chosen])
