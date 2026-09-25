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
The entry point: read the ini, build the application, serve TLS and plain together.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import argparse
import asyncio
import os
import sys

from trix.common import logging as trixlog
from trix.common import settings as configuration
from trix.common.constants import DAEMON_NAME, DAEMON_VERSION


def build_config(config):
    """
    Hypercorn serves the TLS bind and the plain one from the same process.
    """
    from hypercorn.config import Config

    served = Config()
    served.bind = []
    served.insecure_bind = []

    if config.api.certfile and config.api.keyfile:
        if not os.path.isfile(config.api.certfile):
            raise SystemExit(f"certificate {config.api.certfile} is missing")
        served.bind = [config.api.bind]
        served.certfile = config.api.certfile
        served.keyfile = config.api.keyfile
    if config.api.insecure_bind:
        served.insecure_bind = [config.api.insecure_bind]
    if not served.bind and not served.insecure_bind:
        raise SystemExit("neither BIND nor INSECURE_BIND is usable; check trix.ini")

    served.workers = 1
    served.accesslog = "-"
    served.errorlog = "-"
    served.graceful_timeout = 10
    return served


def serve(config_file: str = "") -> int:
    """
    Load, build and run until asked to stop.
    """
    from hypercorn.asyncio import serve as hypercorn_serve

    config = configuration.set_current(configuration.load(config_file))
    logger = trixlog.setup(config.logger.level, config.logger.logfile)
    logger.info("%s %s starting from %s", DAEMON_NAME, DAEMON_VERSION, config.config_file)

    from trix.app import create_app
    application = create_app()
    asyncio.run(hypercorn_serve(application, build_config(config)))
    return 0


def keygen(config_file: str = "") -> int:
    """
    Write this daemon's keypair. The installer calls this, never the daemon.
    """
    from trix.core.security import keys

    config = configuration.set_current(configuration.load(config_file))
    trixlog.setup(config.logger.level, "")
    if config.tokens.shared:
        print(f"[TOKENS] ALGORITHM is {config.tokens.algorithm}, which signs with the "
              f"shared secret and needs no keypair")
        return 1
    if os.path.isfile(config.tokens.private_key):
        print(f"{config.tokens.private_key} already exists; refusing to replace it")
        return 1
    public = keys.generate(config.tokens.private_key)
    print(f"wrote {config.tokens.private_key} and {public}")
    print(f"copy {public} to the other daemon trust directory as "
          f"{config.tokens.key_id}.pub")
    return 0


def main(argv: list = None) -> int:
    """
    Command line for the unit and for the installer.
    """
    parser = argparse.ArgumentParser(prog=DAEMON_NAME, description=__doc__)
    parser.add_argument("--config", default="", help="path to trix.ini")
    parser.add_argument("--version", action="version", version=DAEMON_VERSION)
    parser.add_argument("command", nargs="?", default="serve",
                        choices=("serve", "keygen"),
                        help="serve the API, or write the signing keypair")
    args = parser.parse_args(argv)

    if args.command == "keygen":
        return keygen(args.config)
    return serve(args.config)


if __name__ == "__main__":
    sys.exit(main())
