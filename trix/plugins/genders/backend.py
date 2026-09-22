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
The genders file, from the same node list the slurm blocks are built from.
"""

__author__      = 'Sumit Sharma'
__copyright__   = 'Copyright 2026, TrinityX Project'
__license__     = 'GPL'
__version__     = '1.0'
__maintainer__  = 'Sumit Sharma'
__email__       = 'sumit.sharma@clustervision.com'
__status__      = 'Development'

from trix.common import logging as trixlog
from trix.common.errors import BackendFailure
from trix.plugins.slurm.backend import node_set
from trix.utils import declarations

LOGGER = trixlog.get('genders')

PLUGIN: str = 'genders'

DEFAULT_CONFIG: dict = {
    'genders_file': '/etc/genders',
    'manager': 'TrinityX',
}


def _generator():
    """The packaged generator, imported only when a render actually runs."""
    try:
        from trinityx_config_genders import Generate
    except ImportError as error:
        raise BackendFailure(
            f'trinityx_config_genders is not installed on this controller: {error}') from None
    return Generate()


class Backend:
    """A thin shell around the package."""

    def config(self, body: dict = None) -> dict:
        """Where this plugin writes."""
        if body:
            return declarations.merge(PLUGIN, 'config', '', body, DEFAULT_CONFIG)
        return declarations.read(PLUGIN, 'config', '', DEFAULT_CONFIG)

    def render_genders(self) -> dict:
        """Write the genders file from Luna's nodes."""
        fullset = node_set()
        if not fullset:
            LOGGER.info('no nodes yet, so nothing to write')
            return {'nodes': 0, 'file': self.config()['genders_file']}
        if not _generator().Genders(fullset):
            raise BackendFailure('the genders generator reported a failure')
        LOGGER.info('wrote the genders file for %d node(s)', len(fullset))
        return {'nodes': len(fullset), 'file': self.config()['genders_file']}
