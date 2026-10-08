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
The luna plugin: the one route to Luna. /api/v1/luna/xyz goes to Luna as /xyz, as sent,
and Luna decides who may. A node renamed, moved or deleted through it then runs the
Luna hooks other plugins declared.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import requests
import urllib3
from fastapi import Request, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from trixd.common import logging as trixlog
from trixd.common.errors import ServiceUnavailable
from trixd.core.luna import config as luna_config
from trixd.core.luna import hooks

LOGGER = trixlog.get("plugin.luna")

urllib3.disable_warnings()

HOP_BY_HOP: tuple = ("connection", "keep-alive", "transfer-encoding", "upgrade",
                     "content-length", "content-encoding", "host")


def _forward(method: str, url: str, headers: dict, body: bytes):
    """
    The blocking call itself, run in the worker pool.
    """
    try:
        return requests.request(method, url, headers=headers, data=body or None,
                                timeout=luna_config.TIMEOUT,
                                verify=luna_config.current().verify)
    except requests.RequestException as error:
        raise ServiceUnavailable(f"Luna is not answering: {error}") from None


class Backend:
    """
    One handler: forward.
    """

    async def forward(self, rest: str, request: Request) -> Response:
        """
        Method, query, headers and body as sent, less the connection headers, to the Luna
        luna.ini names. The hooks run once the answer has left.
        """
        url = f"{luna_config.current().base}/{rest}"
        if request.url.query:
            # As sent: a parsed copy would keep only the last of a repeated key.
            url = f"{url}?{request.url.query}"
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in HOP_BY_HOP}
        body = await request.body()
        answer = await run_in_threadpool(_forward, request.method, url, headers, body)
        passed = {key: value for key, value in answer.headers.items()
                  if key.lower() not in HOP_BY_HOP}
        return Response(content=answer.content, status_code=answer.status_code,
                        headers=passed, media_type=answer.headers.get("content-type"),
                        background=BackgroundTask(hooks.after, request.method, rest, body,
                                                  answer.status_code))
