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
The reserved luna keyword: one address, and Luna still decides its own answers.
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
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from trix.common import logging as trixlog
from trix.common.constants import API_PREFIX, RESERVED_PLUGIN
from trix.common.errors import ServiceUnavailable
from trix.common.settings import current as settings

LOGGER = trixlog.get("proxy")

urllib3.disable_warnings()

router = APIRouter(prefix=f"{API_PREFIX}/{RESERVED_PLUGIN}", tags=[RESERVED_PLUGIN])

HOP_BY_HOP: tuple = ("connection", "keep-alive", "transfer-encoding", "upgrade",
                     "content-length", "content-encoding", "host")


def _forward(method: str, url: str, headers: dict, params: dict, body: bytes):
    """
    The blocking call itself, run in the worker pool.
    """
    config = settings().luna
    try:
        return requests.request(method, url, headers=headers, params=params, data=body or None,
                                timeout=config.timeout, verify=config.verify_certificate)
    except requests.RequestException as error:
        raise ServiceUnavailable(f"Luna is not answering: {error}") from None


async def forward(path: str, request: Request) -> Response:
    """
    Hand /api/v1/luna/xyz to Luna as /xyz, byte for byte, enforcement left to Luna.
    The call blocks, so it runs in the worker pool rather than on the event loop.
    """
    config = settings().luna
    url = f"{config.protocol}://{config.endpoint}/{path}"
    headers = {key: value for key, value in request.headers.items()
               if key.lower() not in HOP_BY_HOP}
    body = await request.body()

    answer = await run_in_threadpool(
        _forward, request.method, url, headers, dict(request.query_params), body)

    passed = {key: value for key, value in answer.headers.items()
              if key.lower() not in HOP_BY_HOP}
    return Response(content=answer.content, status_code=answer.status_code,
                    headers=passed, media_type=answer.headers.get("content-type"))


# One route per verb, each with its own operation id. One route carrying four
# methods gives them all the same generated id, which the document cannot express.
for _verb in ("GET", "POST", "PATCH", "DELETE"):
    router.add_api_route(
        "/{path:path}", forward, methods=[_verb],
        name=f"luna_forward_{_verb.lower()}",
        operation_id=f"luna_forward_{_verb.lower()}",
        summary=f"Forward {_verb} to Luna, unchanged")
