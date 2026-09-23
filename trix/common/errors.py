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
Typed errors and the handlers that turn them into responses.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class TrixError(Exception):
    """Base class for every error the daemon raises deliberately."""

    status_code: int = 500

    def __init__(self, message: str = '') -> None:
        super().__init__(message)
        self.message = message or self.__class__.__name__


class BadRequest(TrixError):
    """The request cannot be acted on as written."""

    status_code = 400


class Unauthenticated(TrixError):
    """No token, an expired one, or one from an issuer this daemon does not trust."""

    status_code = 401


class Forbidden(TrixError):
    """The principal is known and lacks the capability the endpoint declared."""

    status_code = 403


class NotFound(TrixError):
    """No such object."""

    status_code = 404


class Conflict(TrixError):
    """The request cannot be applied to the current state, and nothing changed."""

    status_code = 409


class PluginRefused(Conflict):
    """A plugin failed validation, so the previously loaded state was kept."""


class BackendFailure(TrixError):
    """The thing behind the plugin failed: obol, systemctl, a render, Luna."""

    status_code = 502


class ServiceUnavailable(TrixError):
    """A dependency the daemon needs is not answering."""

    status_code = 503


def register(app: FastAPI) -> None:
    """
    Attach one handler for the whole hierarchy.
    """

    @app.exception_handler(TrixError)
    async def _handler(request: Request, exc: TrixError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={'message': exc.message})
