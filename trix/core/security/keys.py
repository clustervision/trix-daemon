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
The signing key this daemon holds, and the public keys it is configured to trust.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from trix.common import logging as trixlog
from trix.common.errors import TrixError

LOGGER = trixlog.get("keys")

PUBLIC_SUFFIX: str = ".pub"


class KeyRing:
    """
    One private key to sign with, and a public key per trusted key identifier.
    """

    def __init__(self, key_id: str = "", issuer: str = "") -> None:
        self.key_id = key_id
        self.issuer = issuer
        self.private = None
        self.trusted: dict = {}

    def load(self, private_path: str, trust_dir: str) -> "KeyRing":
        """
        Read the private key and every public key in the trust directory.
        """
        self.private = read_private(private_path)
        self.trusted = read_trust(trust_dir)
        self.trusted[self.key_id] = self.private.public_key()
        LOGGER.info("signing as %s with key %s, trusting %s",
                    self.issuer, self.key_id, ", ".join(sorted(self.trusted)) or "nothing else")
        return self

    def public_key(self, key_id: str):
        """
        The public key for a key identifier, or None when it is not trusted.
        """
        return self.trusted.get(key_id)


def generate(private_path: str, public_path: str = "") -> str:
    """
    Write a new keypair. Called at bootstrap, never by the running daemon.
    """
    private = ed25519.Ed25519PrivateKey.generate()
    os.makedirs(os.path.dirname(private_path), exist_ok=True)
    blob = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    with open(private_path, "wb") as handle:
        handle.write(blob)
    os.chmod(private_path, 0o600)

    public_path = public_path or private_path + PUBLIC_SUFFIX
    with open(public_path, "wb") as handle:
        handle.write(private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo))
    os.chmod(public_path, 0o644)
    return public_path


def read_private(path: str):
    """
    Load the private key, or say plainly that the daemon cannot mint anything.
    """
    if not os.path.isfile(path):
        raise TrixError(f"no signing key at {path}; the installer generates it at bootstrap")
    with open(path, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise TrixError(f"{path} is not an Ed25519 private key")
    return key


def read_trust(directory: str) -> dict:
    """
    Every <key-id>.pub in the trust directory, indexed by key identifier.
    """
    trusted = {}
    if not os.path.isdir(directory):
        LOGGER.warning("no trust directory at %s: only our own tokens will verify", directory)
        return trusted
    for name in sorted(os.listdir(directory)):
        if not name.endswith(PUBLIC_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as handle:
                key = serialization.load_pem_public_key(handle.read())
        except (OSError, ValueError) as error:
            LOGGER.error("ignoring %s: %s", path, error)
            continue
        if not isinstance(key, ed25519.Ed25519PublicKey):
            LOGGER.error("ignoring %s: not an Ed25519 public key", path)
            continue
        trusted[name[:-len(PUBLIC_SUFFIX)]] = key
    return trusted


_ring: KeyRing = KeyRing()


def set_current(ring: KeyRing) -> KeyRing:
    """
    Hold the key ring the running daemon loaded.
    """
    global _ring
    _ring = ring
    return _ring


def current() -> KeyRing:
    """
    The key ring the running daemon loaded.
    """
    return _ring
