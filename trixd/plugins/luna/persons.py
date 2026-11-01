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
A person signed in to trixd reaches Luna through the route to Luna as themselves: with the
Luna token in ~/.luna/token, or one signed in for with their own ~/.luna/luna.ini, or by
trixd's delegate account on their behalf, kept where the Luna command line keeps it. Never
with the daemon's own token, and no password is ever written.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import os
import threading
import time
from configparser import Error as IniError
from configparser import RawConfigParser

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER, TOKEN_KIND_USER
from trixd.common.errors import (BackendFailure, BadRequest, Forbidden, ServiceUnavailable,
                                 TrixdError, Unauthenticated)
from trixd.common.settings import current as settings
from trixd.core.security import access, principals, tokens
from trixd.db import engine
from trixd.plugins.luna import credentials
from trixd.utils import files

LOGGER = trixlog.get("luna.persons")

# The sources that vouch for an operating-system account, so the name is that account's
# and the home is the person's own. A local principal is only trixd's name.
HOME_SOURCES: frozenset = frozenset({"pam", "ldap"})
# Where the Luna command line keeps a person's login and token, under their home.
FOLDER: str = ".luna"
TOKEN_FILE: str = "token"
LOGIN_FILE: str = "luna.ini"
SIGN_IN: str = "token"
# A home on a file system that has stopped answering holds whoever touches it, so only this
# many at once, and a person whose turn does not come answers 503 rather than queueing.
HOME_CALLS = threading.BoundedSemaphore(4)
HOME_WAIT: float = 1.0
# A delegated sign-in Luna turned down is not asked again for that person before this.
DELEGATE_COOL_OFF: float = 60.0
DELEGATED: str = "delegated"
# This plugin's entry among trixd.ini's [DELEGATE:<component>] sections.
DELEGATE: str = "LUNA"

_lock = threading.Lock()
_signing: dict = {}
# Only for a person whose home cannot keep it: name -> (token, what it was obtained under).
_held: dict = {}
# What Luna turned down -> until when: (name, file, mtime_ns) until the file changes, and
# (name, DELEGATED) for the cool-off.
_refused: dict = {}
_warned: set = set()
_said_no_delegate: list = []


class Person:
    """
    Who a request to Luna comes from, and their passwd entry when they have a home here.
    """

    def __init__(self, name: str, home) -> None:
        """
        The name trixd knows them by.
        """
        self.name = name
        self.home = home


def caller(headers: dict):
    """
    The person behind a request to Luna, or None for whatever passes as it was sent: no
    token, one this daemon cannot verify, one Luna minted, a node's, the ini account's.
    """
    token = headers.get(TOKEN_HEADER) or _bearer(headers.get("authorization", ""))
    if not token:
        return None
    try:
        claims = tokens.verify(token)
    except TrixdError:
        return None
    if ("kind" not in claims.raw or claims.kind != TOKEN_KIND_USER
            or access.is_rootus(claims.subject)):
        return None
    handle = engine.session()
    try:
        who = principals.resolve(handle, claims.subject)
    finally:
        handle.close()
    if who is None:
        raise Unauthenticated(f"{claims.subject} cannot be resolved here")
    return Person(who.name, _home(who.name, claims.source))


def as_person(headers: dict, token: str) -> dict:
    """
    The headers with the person's Luna token in place of whatever they sent.
    """
    passed = {key: value for key, value in headers.items()
              if key.lower() not in (TOKEN_HEADER, "authorization")}
    passed[TOKEN_HEADER] = token
    return passed


def token_for(person: Person) -> str:
    """
    The person's Luna token: ~/.luna/token, else the one held for them, else signed in for,
    one sign-in at a time per person.
    """
    found = _from_home(person) or _from_memory(person)
    if found:
        return found
    with _lock:
        signing = _signing.setdefault(person.name, threading.Lock())
    with signing:
        # Another call for this person may have signed in while this one waited.
        found = _from_home(person) or _from_memory(person)
        if found:
            return found
        return _sign_in_for(person)


def sign_in(person: Person, body: bytes) -> dict:
    """
    POST /token at Luna for the person themselves, the token kept as the command line keeps
    it and the password nowhere. The answer says only that it worked.
    """
    try:
        asked = json.loads(body or b"{}")
        username, password = asked.get("username"), asked.get("password")
    except (ValueError, AttributeError):
        username = password = None
    if not isinstance(username, str) or not isinstance(password, str) or not password:
        raise BadRequest('send {"username": "...", "password": "..."}')
    if username != person.name:
        raise Forbidden(f"{person.name} may sign in at Luna only as {person.name}")
    _keep(person, credentials._sign_in(username, password,
                                       refused=_ask(person, "Luna refused the credentials for")))
    return {"message": f"signed in at Luna as {person.name}"}


def _home(name: str, source: str):
    """
    The passwd entry of a person whose source vouches for an operating-system account that
    could log in itself, spelt exactly as they signed in, never uid 0; otherwise None.
    """
    if source not in HOME_SOURCES:
        return None
    try:
        from trixd.core.security.sources.pam import Source
        known, _ = Source().resolve(name)
        import pwd
        entry = pwd.getpwnam(name) if known else None
    except (ImportError, KeyError) as error:
        _warn_once(name, f"no home is looked up: {type(error).__name__}")
        return None
    if entry is None or entry.pw_uid == 0 or entry.pw_name != name:
        return None
    return entry


def _bearer(value: str) -> str:
    """
    The token out of an Authorization: Bearer header.
    """
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _ask(person: Person, why: str) -> Unauthenticated:
    """
    The 401 that tells the client to ask the person for their Luna credentials.
    """
    return Unauthenticated(f"{why} {person.name}; sign in at Luna with POST "
                           f"{API_PREFIX}/luna/{SIGN_IN} and your username and password")


def _warn_once(name: str, reason: str) -> None:
    """
    Say once for each person, not on every call, why their files are not used.
    """
    with _lock:
        if name in _warned:
            return
        _warned.add(name)
    LOGGER.warning("%s: %s", name, reason)


def _at_home(person: Person, work, *args):
    """
    One piece of work in a home, waiting at most HOME_WAIT for a turn.
    """
    if not HOME_CALLS.acquire(timeout=HOME_WAIT):
        raise ServiceUnavailable(f"the home of {person.name} is not answering")
    try:
        return work(*args)
    finally:
        HOME_CALLS.release()


def _read(person: Person, name: str) -> tuple:
    """
    One of the person's files under ~/.luna as (text, mtime_ns), or ("", 0) when it is not
    there or not theirs to be read from.
    """
    if person.home is None:
        return "", 0
    try:
        return _at_home(person, files.read_private, person.home.pw_dir, FOLDER, name,
                        person.home.pw_uid)
    except BackendFailure as error:
        _warn_once(person.name, error.message)
        return "", 0


def _from_home(person: Person) -> str:
    """
    ~/.luna/token, when it holds a token worth presenting.
    """
    found = _read(person, TOKEN_FILE)[0].strip()
    return found if credentials._usable(found) else ""


def _from_memory(person: Person) -> str:
    """
    The token held for a person whose home could not keep it.
    """
    with _lock:
        token, obtained = _held.get(person.name, ("", ()))
    if obtained == credentials._fingerprint() and credentials._usable(token):
        return token
    return ""


def _sign_in_for(person: Person) -> str:
    """
    The person's own ~/.luna/luna.ini when it names them, else the delegate account on
    their behalf. What Luna turned down is not offered again until the file changes, or for
    a delegated sign-in until the cool-off has passed.
    """
    text, stamp = _read(person, LOGIN_FILE)
    if stamp:
        username, password = _account(person, text)
        if username == person.name and password:
            return _signed_in(person, (person.name, LOGIN_FILE, stamp), float("inf"),
                              _ask(person, "Luna refused ~/.luna/luna.ini for"),
                              username, password)
        _warn_once(person.name, f"{os.path.join(person.home.pw_dir, FOLDER, LOGIN_FILE)} "
                                f"does not name {person.name}; it is not used")
    username, password = settings().delegates.get(DELEGATE, ("", ""))
    if not username or not password:
        if not _said_no_delegate:
            _said_no_delegate.append(True)
            LOGGER.warning("trixd.ini has no [DELEGATE:%s] USERNAME and PASSWORD, so a person "
                           "with no Luna token of their own signs in at Luna themselves",
                           DELEGATE)
        raise _ask(person, "no Luna token is kept for")
    return _signed_in(person, (person.name, DELEGATED), time.monotonic() + DELEGATE_COOL_OFF,
                      _ask(person, "Luna would not sign in on behalf of"),
                      username, password, on_behalf_of=person.name)


def _signed_in(person: Person, seen: tuple, until: float, refusal: Unauthenticated,
               username: str, password: str, on_behalf_of: str = "") -> str:
    """
    Sign in at Luna unless Luna has turned these credentials down and the refusal stands,
    keep the token, and remember a refusal until `until`.
    """
    now = time.monotonic()
    with _lock:
        if _refused.get(seen, 0) > now:
            raise refusal
    try:
        token = credentials._sign_in(username, password, refused=refusal,
                                     on_behalf_of=on_behalf_of)
    except Unauthenticated:
        with _lock:
            for stale in [key for key, ends in _refused.items() if ends <= now]:
                del _refused[stale]
            _refused[seen] = until
        raise
    _keep(person, token)
    return token


def _account(person: Person, text: str) -> tuple:
    """
    [API] USERNAME and PASSWORD out of ~/.luna/luna.ini. What the parser objects to is
    not repeated, because the line it quotes may be the password.
    """
    parser = RawConfigParser()
    try:
        parser.read_string(text)
    except IniError as error:
        _warn_once(person.name, f"{os.path.join(person.home.pw_dir, FOLDER, LOGIN_FILE)} "
                                f"cannot be read: {type(error).__name__}")
        return "", ""
    return (parser.get("API", "USERNAME", fallback="").strip(),
            parser.get("API", "PASSWORD", fallback="").strip())


def _keep(person: Person, token: str) -> None:
    """
    Write the token where the command line keeps it. A home that cannot take it leaves the
    token held here until it expires, said once per person.
    """
    reason = "has no home on this controller"
    if person.home is not None:
        try:
            _at_home(person, files.write_private, person.home.pw_dir, FOLDER, TOKEN_FILE,
                     token, person.home.pw_uid, person.home.pw_gid)
            reason = ""
        except BackendFailure as error:
            reason = error.message
    with _lock:
        if reason:
            for name in [name for name, (held, _) in _held.items()
                         if not credentials._usable(held)]:
                del _held[name]
            _held[person.name] = (token, credentials._fingerprint())
        else:
            _held.pop(person.name, None)
            _warned.discard(person.name)
            for seen in [seen for seen in _refused if seen[0] == person.name]:
                del _refused[seen]
    if reason:
        _warn_once(person.name, f"the Luna token is held in memory until it expires: {reason}")
