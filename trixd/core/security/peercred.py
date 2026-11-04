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
The peercred socket: a person's own process connects, the kernel says which uid it runs
as, and the answer is a trixd token for that account. Nothing else of the API is here.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import asyncio
import json
import os
import pwd
import socket
import stat
import struct
import threading

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX
from trixd.common.settings import current as settings
from trixd.core.security import audit, principals, tokens
from trixd.db import engine as database

LOGGER = trixlog.get("auth.peercred")

SOURCE: str = "peercred"
PATH: str = f"{API_PREFIX}/auth/token"
HEAD_LIMIT: int = 8192
READ_WAIT: float = 5.0
# A name service or PAM stack that has stopped answering holds whoever asks it, so only
# this many at once, and a caller whose turn does not come answers 503 rather than queueing.
CALLS = threading.BoundedSemaphore(4)
REASONS: dict = {201: "Created", 400: "Bad Request", 401: "Unauthorized",
                 404: "Not Found", 503: "Service Unavailable"}


async def start():
    """
    Open the socket when [AUTH] CHAIN names peercred. One that cannot be opened is logged
    and the rest of the daemon serves on, as a source that cannot work is skipped.
    """
    path = settings().auth.socket
    if SOURCE not in settings().auth.chain:
        return None
    try:
        if os.path.lexists(path) and stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)
        server = await asyncio.start_unix_server(_serve, path=path, limit=HEAD_LIMIT)
        # Anyone may connect: the kernel, not the caller, says who they are.
        os.chmod(path, 0o666)
    except OSError as error:
        LOGGER.error("peercred is in [AUTH] CHAIN but %s cannot be opened, so nobody can "
                     "sign in through it: %s", path, error)
        return None
    LOGGER.info("peercred sign-in on %s", path)
    return server


def stop(server) -> None:
    """
    Stop accepting; the next start removes the socket file.
    """
    if server is not None:
        server.close()


async def _serve(reader, writer) -> None:
    """
    One request, one answer, and the connection closes.
    """
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_WAIT)
        line = head.split(b"\r\n", 1)[0].decode("latin-1").split()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        status, body = 400, {"message": "send one HTTP request"}
    else:
        if len(line) == 3 and line[0] in ("GET", "POST") and line[1].split("?")[0] == PATH:
            status, body = await _token(writer)
        else:
            status, body = 404, {"message": f"only {PATH} is served here"}
    payload = json.dumps(body).encode()
    writer.write(f"HTTP/1.1 {status} {REASONS[status]}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                 + payload)
    try:
        await writer.drain()
    except ConnectionError as error:
        LOGGER.debug("peercred caller left before the answer: %s", error)
    finally:
        writer.close()


async def _token(writer) -> tuple:
    """
    Who is calling, as the kernel reports it, answered off the event loop.
    """
    pid, uid = _peer(writer)
    if not CALLS.acquire(blocking=False):
        LOGGER.warning("peercred is busy; uid %d, pid %d is asked to try again", uid, pid)
        return 503, {"message": "trixd is busy signing others in; try again"}
    return await asyncio.get_running_loop().run_in_executor(None, _issue, uid, pid)


def _peer(writer) -> tuple:
    """
    The pid and uid of the process at the other end, from SO_PEERCRED.
    """
    pid, uid, _ = struct.unpack("3i", writer.get_extra_info("socket").getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return pid, uid


def _issue(uid: int, pid: int) -> tuple:
    """
    A token for the account behind the uid, or a refusal in the log and the audit trail.
    """
    try:
        name, why = _name(uid)
        who = None
        if name:
            handle = database.session()
            try:
                who, why = principals.vouched(handle, name, SOURCE)
            finally:
                handle.close()
        if who is None:
            LOGGER.warning("peercred refused uid %d, pid %d: %s", uid, pid, why)
            audit.refused(name or f"uid {uid}", "auth.token",
                          detail=f"through peercred, pid {pid}: {why}")
            return 401, {"message": "this account may not sign in to trixd"}
        audit.granted(who.name, "auth.token", detail=f"through peercred, pid {pid}")
        return 201, {"token": tokens.mint(who.name, expiry=settings().api.expiry,
                                          source=SOURCE)}
    except Exception as error:
        LOGGER.error("peercred could not sign in uid %d, pid %d: %s", uid, pid, error)
        return 503, {"message": "trixd cannot sign anybody in through this socket now"}
    finally:
        CALLS.release()


def _name(uid: int) -> tuple:
    """
    The name the uid has, when that name leads back to the same uid; never uid 0.
    """
    if uid == 0:
        return "", "uid 0 never signs in this way"
    try:
        name = pwd.getpwuid(uid).pw_name
        same = pwd.getpwnam(name).pw_uid == uid
    except KeyError:
        name, same = "", False
    return (name, "") if same else ("", f"uid {uid} has no name that leads back to it")
