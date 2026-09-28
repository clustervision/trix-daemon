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

from trix.common import logging as trixlog
from trix.db import engine as database
from trix.db import models

LOGGER = trixlog.get("audit")

GRANTED: str = "granted"
REFUSED: str = "refused"

DETAIL_LIMIT: int = 1024


def record(principal: str, action: str, object_name: str = "",
           outcome: str = GRANTED, detail: str = "") -> None:
    """
    One row on its own session, so a failed request still leaves its trace and a
    write here is never the reason an otherwise good request fails.
    """
    handle = database.session()
    try:
        handle.add(models.Audit(principal=principal or "anonymous", action=action,
                                object=object_name or "", outcome=outcome,
                                detail=(detail or "")[:DETAIL_LIMIT]))
        handle.commit()
    except Exception as error:
        handle.rollback()
        LOGGER.error("could not record %s %s by %s: %s", action, object_name,
                     principal, error)
    finally:
        handle.close()


def granted(principal: str, action: str, object_name: str = "", detail: str = "") -> None:
    """
    Something a caller was allowed to do.
    """
    record(principal, action, object_name, GRANTED, detail)


def refused(principal: str, action: str, object_name: str = "", detail: str = "") -> None:
    """
    Something a caller was not allowed to do, which is the half worth having.
    """
    record(principal, action, object_name, REFUSED, detail)
