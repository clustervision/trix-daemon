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
The one place the daemon decides what a certificate is checked against.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import ipaddress
import ssl
from urllib.parse import urlsplit

import requests


def system_trust() -> str:
    """
    The certificate store the operating system keeps, which is where the cluster CA is
    installed, and which is not the bundle `requests` reaches for on its own: inside a
    virtual environment that one is certifi's, and certifi has never heard of the cluster.

    Read on every call rather than once, so a store installed after the daemon started is
    used without a restart.

    A system with no store at all raises instead of answering, because both answers it
    could give are worse: True is certifi again, and an empty path is how `requests`
    spells "do not check". It raises what `requests` itself raises for a certificate that
    cannot be verified, so it arrives where every caller already handles that and nothing
    has to learn a new exception.

    The companion directory is not a substitute: OpenSSL reads it by hashed name only, and
    the platform leaves those out.
    """
    store = ssl.get_default_verify_paths().cafile or ""
    if not store:
        raise requests.exceptions.SSLError(
            "no certificate store was found on this system to verify against")
    return store


def other_scheme(url: str) -> str:
    """
    The same address over the other scheme, for the one probe that answers whether a
    service wanted TLS after all.
    """
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return "https://" + url.split("://", 1)[-1]


def is_loopback(url: str) -> bool:
    """
    Whether an address is the box's own, which is the only one a controller certificate is
    not expected to carry.
    """
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
