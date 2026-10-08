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
Writing the audit trail. Refusals are recorded too, or it answers the wrong question.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trixd.common import logging as trixlog
from trixd.db import engine as database
from trixd.db import models

LOGGER = trixlog.get("audit")

GRANTED: str = "granted"
REFUSED: str = "refused"

DETAIL_LIMIT: int = 1024


def record(principal: str, action: str, object_name: str = "",
           outcome: str = GRANTED, detail: str = "", handle=None) -> None:
    """
    One row. Pass the request's own session where it holds one, or SQLite blocks
    on its own write lock until the busy timeout and the row is lost.
    """
    row = models.Audit(principal=principal or "anonymous", action=action,
                       object=object_name or "", outcome=outcome,
                       detail=(detail or "")[:DETAIL_LIMIT])
    if handle is not None:
        handle.add(row)
        return
    own = database.session()
    try:
        own.add(row)
        own.commit()
    except Exception as error:
        own.rollback()
        LOGGER.error("could not record %s %s by %s: %s", action, object_name,
                     principal, error)
    finally:
        own.close()


def granted(principal: str, action: str, object_name: str = "", detail: str = "",
            handle=None) -> None:
    """
    Something a caller was allowed to do.
    """
    record(principal, action, object_name, GRANTED, detail, handle)


def refused(principal: str, action: str, object_name: str = "", detail: str = "",
            handle=None) -> None:
    """
    A refusal always gets its own session: the request it belongs to is about to
    roll back, and a row on that session would roll back with it.
    """
    record(principal, action, object_name, REFUSED, detail)
