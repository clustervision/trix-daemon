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
A ceiling on how much of a request this daemon will read.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import json

from trixd.common import logging as trixlog
from trixd.common.errors import TooLarge
from trixd.common.settings import current as settings

LOGGER = trixlog.get("limits")


class BodyLimit:
    """
    Refuse a request body past `[API] MAX_BODY`, before anything reads it.

    The framework reads the whole body before it solves a route's dependencies, so the gate
    runs after it. Without a ceiling an unauthenticated caller decides how much memory one
    request costs, and the refusal arrives having already paid for what it refuses.

    A declared length past the limit is answered from here and the application is never
    called. A body that declares no length is counted as it arrives: omitting the header is
    all it would take to walk past a check on the header alone.

    The limit is read per request, as the gate reads its own settings, so changing it in
    the ini applies to the next request rather than the next restart. Zero means no ceiling.
    """

    def __init__(self, app) -> None:
        """
        Wrap the application.
        """
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        """
        One request, refused here or passed on with its body counted.
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        maximum = settings().api.max_body
        if maximum <= 0:
            await self.app(scope, receive, send)
            return
        declared = _declared(scope)
        if declared is not None and declared > maximum:
            LOGGER.warning("refused a declared %d byte body on %s %s: the ceiling is %d",
                           declared, scope.get("method", ""), scope.get("path", ""), maximum)
            await _refuse(send, maximum)
            return
        # A body with no declared length is counted as it arrives, and the answer is
        # replaced rather than raised: an exception from receive is caught by the framework
        # while it parses the body and becomes "there was an error parsing the body", which
        # is a 400 blaming the caller's syntax for a refusal this made.
        state = {"seen": 0, "over": False, "answered": False}
        await self.app(scope, _counted(receive, maximum, scope, state),
                       _guarded(send, maximum, state))


def _declared(scope) -> int:
    """
    The content length the caller claims, or None where it claims none or claims nonsense.
    """
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _counted(receive, maximum: int, scope, state: dict):
    """
    The same receive, which stops feeding the body once the ceiling has been passed.
    """

    async def counted():
        """
        One ASGI message, with the body so far tallied.
        """
        message = await receive()
        if message.get("type") == "http.request" and not state["over"]:
            state["seen"] += len(message.get("body") or b"")
            if state["seen"] > maximum:
                state["over"] = True
                LOGGER.warning("refused a body on %s %s after %d bytes: the ceiling is %d",
                               scope.get("method", ""), scope.get("path", ""),
                               state["seen"], maximum)
                # The application is told the caller went away, which is true enough: it
                # will get no more of this body. Whatever it answers is replaced below.
                return {"type": "http.disconnect"}
        return message

    return counted


def _guarded(send, maximum: int, state: dict):
    """
    The same send, answering 413 instead of whatever the application made of a cut body.
    """

    async def guarded(message) -> None:
        """
        One response message, passed on or replaced.
        """
        if not state["over"]:
            await send(message)
            return
        if state["answered"]:
            return
        if message.get("type") == "http.response.start":
            state["answered"] = True
            await _refuse(send, maximum)

    return guarded


async def _refuse(send, maximum: int) -> None:
    """
    Say no without reading what was offered.
    """
    body = json.dumps({"message": f"the request body is larger than {maximum} bytes, which "
                                  f"is what [API] MAX_BODY allows"}).encode("utf-8")
    await send({"type": "http.response.start", "status": TooLarge.status_code,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii"))]})
    await send({"type": "http.response.body", "body": body})


def register(app) -> None:
    """
    Put the ceiling outside everything else, so it is paid before any of it.
    """
    app.add_middleware(BodyLimit)
