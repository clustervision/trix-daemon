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
One logger for the daemon, to the journal and optionally to a file.
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
import sys

LOGGER_NAME: str = "trixd"
FORMAT: str = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

LEVELS: dict = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def setup(level: str = "info", logfile: str = "") -> logging.Logger:
    """
    Configure the daemon logger. Safe to call more than once.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(LEVELS.get(str(level).lower(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(FORMAT))
    logger.addHandler(stream)

    if logfile:
        try:
            os.makedirs(os.path.dirname(logfile), exist_ok=True)
            handler = logging.FileHandler(logfile, encoding="utf-8")
            handler.setFormatter(logging.Formatter(FORMAT))
            logger.addHandler(handler)
        except OSError as error:
            # The journal handler is already attached, so say why the file is
            # missing rather than failing a start over a log path.
            logger.warning("cannot write %s: %s", logfile, error)
    return logger


def get(name: str = "") -> logging.Logger:
    """
    The daemon logger, or a named child of it.
    """
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def report(messages: list) -> None:
    """
    Say what reading the ini found, now that there is somewhere to say it. Called once per
    command, so one problem in the ini is reported once however often it was read.

    Attributed to the settings module, which found them and is the name they already
    carried: what had to change is where these lines land, not what a site greps to find
    them. The logger is taken here rather than held in that module, which has none on
    purpose - nothing it could log to exists while the ini is being read.
    """
    logger = get("settings")
    for message in messages:
        logger.warning("%s", message)
