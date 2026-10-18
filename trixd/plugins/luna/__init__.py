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
The luna plugin: the one route to Luna, /api/v1/luna, and everything else that knows
what Luna is. Importing it fills the daemon's slots, so a daemon without this plugin
simply has no Luna, which is the point of it being a plugin.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

# Every one by its canonical name: the loader also imports this directory under a module
# name of its own, and a relative import here would give that copy its own client and its
# own token. One name, one client, one cache.
from trixd.api.v1 import docs
from trixd.core.render import triggers
from trixd.core.security import nodes
from trixd.plugins.luna import config as luna_config
from trixd.plugins.luna import hashes
from trixd.plugins.luna.client import current as client
from trixd.plugins.luna.credentials import (get_credentials, get_token,
                                            get_token_for)

# What the published document shows, where no reader's host is known.
_PUBLISHED: str = "https://<controller>:7050"


def _link(request) -> str:
    """
    Luna on the host the reader reached the document by: it runs on the same controller,
    and the name luna.ini gives it may not resolve where the reader sits.
    """
    config = luna_config.current()
    port = config.endpoint.rpartition(":")[2] or "7050"
    host = request.url.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    return f"{config.protocol}://{host}:{port}"


def install() -> None:
    """
    Plug this plugin in. The loader calls it once the plugin has loaded.

    Here rather than at import, which is where it used to be. Another plugin reaching for
    the Luna client - `from trixd.plugins.luna.client import current` in the Prometheus and
    slurm backends - imports this package on the way, so importing it registered the node
    source and started the Luna poller for a plugin that was never loaded and that nobody
    had enabled: trixd went on polling Luna and issuing provision-scoped node tokens while
    `/system/plugins` named neither, and nothing on the control surface could stop it.
    Taking luna out of ENABLED, or out of the plugin directory altogether, did not help -
    the import resolves against the Python package path, not the configured directory.

    Owned, both of them, so unplugging this plugin takes the poller and the node source
    with it.
    """
    triggers.register("luna_hashes", hashes.current(), owner="luna")
    nodes.register(client(), owner="luna")
    docs.register_placeholder("luna", _link, _PUBLISHED)


__all__ = ["client", "get_credentials", "get_token", "get_token_for", "install"]
