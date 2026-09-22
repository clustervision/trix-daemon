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
Health, version, the plugin control surface and the notify endpoint Luna's hook calls.
"""

__author__      = 'Sumit Sharma'
__copyright__   = 'Copyright 2026, TrinityX Project'
__license__     = 'GPL'
__version__     = '1.0'
__maintainer__  = 'Sumit Sharma'
__email__       = 'sumit.sharma@clustervision.com'
__status__      = 'Development'

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from trix.common import logging as trixlog
from trix.common.constants import (API_PREFIX, DAEMON_NAME, DAEMON_VERSION,
                                   PLUGIN_STATE_LOADED)
from trix.common.errors import PluginRefused
from trix.core.plugins import loader
from trix.core.plugins.registry import current as registry
from trix.core.render.engine import current as renders
from trix.core.security.gate import Caller, guard

LOGGER = trixlog.get('system')

router = APIRouter(prefix=f'{API_PREFIX}/system', tags=['system'])


class Change(BaseModel):
    """What Luna's config hook posts when something it owns has changed."""

    object: str = ''
    name: str = ''


@router.get('/health', summary='Whether this daemon is serving')
def health() -> dict:
    """Unauthenticated on purpose: a health check that needs a token is not one."""
    loaded = [entry for entry in registry().plugins.values()
              if entry.state == PLUGIN_STATE_LOADED]
    return {'status': 'ok', 'daemon': DAEMON_NAME, 'plugins': len(loaded)}


@router.get('/version', summary='What is running here')
def version() -> dict:
    return {'version': {'trix': DAEMON_VERSION, 'api': 1}}


@router.get('/plugins', summary='Every plugin, its state and why')
def plugins(caller: Caller = Depends(guard('system.read'))) -> dict:
    return {'plugins': registry().listing()}


@router.post('/plugins/{name}/reload', summary='Plug a plugin in, or reload it in place')
def reload_plugin(name: str, request: Request,
                  caller: Caller = Depends(guard('system.write'))) -> dict:
    """Discover, validate and swap. Nothing changes if the plugin is refused."""
    entry = loader.load(request.app, name)
    if entry.state != PLUGIN_STATE_LOADED:
        raise PluginRefused(entry.reason or f'{name} was refused')
    return {'plugin': entry.summary()}


@router.post('/plugins/{name}/disable', summary='Unplug a plugin')
def disable_plugin(name: str, request: Request,
                   caller: Caller = Depends(guard('system.write'))) -> dict:
    """Its paths stop resolving from the next request, and a restart agrees."""
    entry = loader.unload(request.app, name)
    return {'plugin': entry.summary()}


@router.get('/renders', summary='Every declared render and when it last ran')
def render_listing(caller: Caller = Depends(guard('system.read'))) -> dict:
    return {'renders': renders().listing()}


@router.post('/notify', summary='A Luna object changed')
def notify(body: Change, caller: Caller = Depends(guard('system.notify'))) -> dict:
    """Luna's hook posts here and does not wait for the answer.

    Losing one of these is acceptable: the hash comparison on the slow timer is
    the fallback that catches up.
    """
    woken = renders().notify(f'luna.{body.object}' if body.object else '')
    LOGGER.info('notified of %s %s, woke %d render(s)', body.object, body.name, len(woken))
    return {'rendered': woken}
