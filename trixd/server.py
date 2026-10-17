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
import signal
import sys

from trixd.common import logging as trixlog
from trixd.common import settings as configuration
from trixd.common.constants import DAEMON_NAME, DAEMON_VERSION

LOGGER = trixlog.get("server")


def build_config(config):
    """
    Hypercorn serves the TLS bind and the plain one from the same process.
    """
    from hypercorn.config import Config

    served = Config()
    served.bind = []
    served.insecure_bind = []
    bind, plain = config.api.bind, config.api.insecure_bind
    protocol = config.api.protocol

    if bind and protocol == "https":
        for setting, path in (("CERTFILE", config.api.certfile), ("KEYFILE", config.api.keyfile)):
            if not path or not os.path.isfile(path):
                raise SystemExit(f"[API] PROTOCOL is https, so BIND {bind} needs [API] "
                                 f"{setting}; {path or 'it is empty'} is not a file")
        served.bind = [bind]
        served.certfile = config.api.certfile
        served.keyfile = config.api.keyfile
        if plain:
            served.insecure_bind = [plain]
    else:
        # Hypercorn opens insecure_bind only beside a TLS listener, so without TLS every
        # plain listener is a bind.
        served.bind = [item for item in (bind, plain) if item]
        if bind:
            LOGGER.warning("[API] PROTOCOL is http: serving plain HTTP on %s",
                           ", ".join(served.bind))
    if not served.bind:
        raise SystemExit("neither BIND nor INSECURE_BIND is usable; check trixd.ini")

    served.workers = 1
    served.accesslog = "-"
    served.errorlog = "-"
    # Chosen rather than inherited. Start-up runs the migrations, loads every plugin, takes
    # each trigger's baseline and produces every render inside the lifespan, and some of
    # that reaches Luna - which has a timeout of its own. The default is 60s, and exceeding
    # it kills the process, which systemd restarts five seconds later, which starts again
    # and exceeds it again: a loop that never trips the start limiter because each attempt
    # is far longer than the interval it is measured over.
    served.startup_timeout = 300
    served.graceful_timeout = 10
    # Hypercorn closes an idle connection after 5s, racing a client about to reuse it.
    served.keep_alive_timeout = 75
    return served


def serve(config_file: str = "") -> int:
    """
    Load, build and run until asked to stop.
    """
    from hypercorn.asyncio import serve as hypercorn_serve

    config = configuration.set_current(configuration.load(config_file))
    logger = trixlog.setup(config.logger.level, config.logger.logfile)
    logger.info("%s %s starting from %s", DAEMON_NAME, DAEMON_VERSION, config.config_file)

    from trixd.app import create_app
    application = create_app()
    asyncio.run(_run(hypercorn_serve, application, build_config(config)))
    return 0


async def _run(hypercorn_serve, application, hypercorn_config) -> None:
    """
    Serve, with SIGHUP wired to a plugin reload so ExecReload does what it says.
    """
    _on_hangup(application)
    await hypercorn_serve(application, hypercorn_config)


def _on_hangup(application) -> None:
    """
    Reload every plugin from disk on SIGHUP, which is what systemctl reload sends.
    The ini and the listeners are not re-read: a bind change is still a restart.
    """
    if not hasattr(signal, "SIGHUP"):
        return

    def reload_plugins() -> None:
        """
        Discover and swap, logging what each plugin did.
        """
        from trixd.core.plugins import loader

        LOGGER.info("SIGHUP: reloading plugins from disk")
        try:
            states = loader.load_all(application)
        except Exception as error:
            LOGGER.error("SIGHUP reload failed, the running plugins are untouched: %s", error)
            return
        LOGGER.info("SIGHUP: %s", ", ".join(f"{k}={v}" for k, v in states.items()) or "none")

    # In the worker pool: a reload imports and validates, and must not hold the event loop.
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, lambda: loop.run_in_executor(None, reload_plugins))


def keygen(config_file: str = "") -> int:
    """
    Write this daemon's keypair. The installer calls this, never the daemon.
    """
    from trixd.core.security import keys

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


def plugin_new(name: str, config_file: str = "") -> int:
    """
    Write a plugin that loads, into the site's own plugins directory.
    """
    from trixd.core.plugins import scaffold

    config = configuration.set_current(configuration.load(config_file))
    trixlog.setup(config.logger.level, "")
    try:
        written = scaffold.new(name, config.plugins.site_directory, config.plugins.directory)
    except Exception as error:
        print(getattr(error, "message", str(error)))
        return 1
    for path in written:
        print(f"wrote {path}")
    print(f"reload it with: POST /api/v1/system/plugins/{name}/reload")
    print(f"and add {name} to [PLUGINS] ENABLED, or a restart leaves it out")
    return 0


def plugin_check(path: str) -> int:
    """
    Check one plugin directory and print every problem, or that it would load.
    """
    from trixd.core.plugins.check import check

    problems = check(path)
    for problem in problems:
        print(f"{path}: {problem}")
    if not problems:
        print(f"{path}: would load")
    return 1 if problems else 0


def main(argv: list = None) -> int:
    """
    Command line for the unit and for the installer.
    """
    parser = argparse.ArgumentParser(prog=DAEMON_NAME, description=__doc__)
    parser.add_argument("--config", default="", help="path to trixd.ini")
    parser.add_argument("--version", action="version", version=DAEMON_VERSION)
    parser.add_argument("command", nargs="?", default="serve",
                        choices=("serve", "keygen", "plugin"),
                        help="serve the API, write the keypair, or scaffold a plugin")
    parser.add_argument("subcommand", nargs="?", default="",
                        help="for plugin: new or check")
    parser.add_argument("name", nargs="?", default="",
                        help="the plugin name, or for check a plugin directory")
    args = parser.parse_args(argv)
    if args.command == "plugin" and args.subcommand == "check" and os.path.isdir(args.name):
        # A directory needs no trixd.ini: an author or a pipeline checks before installing.
        return plugin_check(args.name)
    try:
        configuration.load(args.config)
    except ValueError as error:
        # A wrong setting stops the daemon here, named, rather than as a traceback later.
        print(f"{DAEMON_NAME}: {error}", file=sys.stderr)
        return 2

    if args.command == "keygen":
        return keygen(args.config)
    if args.command == "plugin":
        if args.subcommand == "check" and args.name:
            from trixd.core.plugins import loader

            configuration.set_current(configuration.load(args.config))
            found = loader.discover().get(args.name, "")
            if not found:
                print(f"no plugin named {args.name}")
                return 1
            return plugin_check(found)
        if args.subcommand != "new" or not args.name:
            print(f"usage: {DAEMON_NAME} plugin new <name> | plugin check <name or directory>")
            return 1
        return plugin_new(args.name, args.config)
    return serve(args.config)


if __name__ == "__main__":
    sys.exit(main())
