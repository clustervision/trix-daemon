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
Walking the authentication sources in the order [AUTH] CHAIN names them.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import importlib

from trix.common import logging as trixlog
from trix.common.settings import current as settings

LOGGER = trixlog.get("auth.chain")

PACKAGE: str = "trix.core.security.sources"


def named() -> list:
    """
    The sources to ask, in order, as the ini names them.
    """
    return list(settings().auth.chain)


def _source(name: str, handle):
    """
    One source, constructed. A source that cannot work raises and the caller skips it.
    """
    module = importlib.import_module(f"{PACKAGE}.{name}")
    return module.Source(handle)


def authenticate(handle, name: str, password: str) -> tuple:
    """
    The first source that knows the name decides, and a refusal stops the walk.
    One that cannot work is skipped, so a local principal still gets in.
    """
    for source in named():
        try:
            answer, result = _source(source, handle).authenticate(name, password)
        except ModuleNotFoundError:
            LOGGER.error("no authentication source named %s; check [AUTH] CHAIN", source)
            continue
        except Exception as error:
            LOGGER.error("authentication source %s is unavailable: %s", source, error)
            continue
        if answer is None:
            LOGGER.debug("source %s does not know %s: %s", source, name, result)
            continue
        if answer is False:
            LOGGER.warning("source %s refused %s: %s", source, name, result)
            return None, source
        return result, source
    return None, ""


def resolve(handle, name: str) -> tuple:
    """
    The first source that knows the name, with no credential checked.
    """
    for source in named():
        try:
            answer, result = _source(source, handle).resolve(name)
        except ModuleNotFoundError:
            LOGGER.error("no authentication source named %s; check [AUTH] CHAIN", source)
            continue
        except Exception as error:
            LOGGER.error("authentication source %s is unavailable: %s", source, error)
            continue
        if answer:
            return result, source
        LOGGER.debug("source %s does not know %s: %s", source, name, result)
    return None, ""
