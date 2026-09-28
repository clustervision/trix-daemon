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
The one place the daemon starts a subprocess.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import subprocess

from trix.common import logging as trixlog
from trix.common.errors import BackendFailure

LOGGER = trixlog.get("shell")

DEFAULT_TIMEOUT: int = 60


class Result:
    """
    What a command did.
    """

    def __init__(self, code: int, out: str, err: str) -> None:
        """
        The exit code and both streams of a finished command.
        """
        self.code = code
        self.out = out
        self.err = err

    @property
    def ok(self) -> bool:
        """
        Whether the command finished with a zero exit code.
        """
        return self.code == 0


def run(argv: list, timeout: int = DEFAULT_TIMEOUT, check: bool = False) -> Result:
    """
    Run a command as an argument list. Never a shell string, always a timeout.
    """
    if not argv or not isinstance(argv, (list, tuple)):
        raise BackendFailure("a command must be given as an argument list")
    LOGGER.debug("running %s", " ".join(str(item) for item in argv))
    try:
        done = subprocess.run([str(item) for item in argv], capture_output=True,
                              timeout=timeout, check=False)
    except FileNotFoundError:
        raise BackendFailure(f"{argv[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise BackendFailure(f"{argv[0]} did not finish within {timeout}s") from None

    result = Result(done.returncode,
                    done.stdout.decode("utf-8", "replace").strip(),
                    done.stderr.decode("utf-8", "replace").strip())
    if check and not result.ok:
        raise BackendFailure(f"{argv[0]} failed: {result.err or result.out}")
    return result


def run_json(argv: list, timeout: int = DEFAULT_TIMEOUT):
    """
    Run a command that answers JSON and return the parsed document.
    """
    result = run(argv, timeout=timeout, check=True)
    if not result.out:
        return None
    try:
        return json.loads(result.out)
    except ValueError as error:
        raise BackendFailure(f"{argv[0]} did not return JSON: {error}") from None
