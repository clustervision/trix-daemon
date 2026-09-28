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
What this daemon signs tokens with: a shared secret, or a keypair it alone holds.
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
from trix.common.constants import MINIMUM_SECRET_LENGTH, TOKEN_ALGORITHM_SHARED
from trix.common.errors import TrixError
from trix.common.settings import TokenSettings

LOGGER = trixlog.get("keys")

PUBLIC_SUFFIX: str = ".pub"


class KeyRing:
    """
    The material one algorithm needs, whichever of the two is configured.
    """

    def __init__(self, config: TokenSettings = None) -> None:
        """
        Hold the settings; nothing is read until load runs.
        """
        self.config = config or TokenSettings()
        self.algorithm: str = self.config.algorithm
        self.key_id: str = self.config.key_id
        self.issuer: str = self.config.issuer
        self.secret: str = ""
        self.private = None
        self.trusted: dict = {}

    @property
    def shared(self) -> bool:
        """
        Whether one secret both signs and verifies, which is what Luna does.
        """
        return self.algorithm == TOKEN_ALGORITHM_SHARED

    def load(self) -> "KeyRing":
        """
        Read whatever the configured algorithm signs with.
        """
        return self._load_secret() if self.shared else self._load_keypair()

    def _load_secret(self) -> "KeyRing":
        """
        Take the secret both daemons hold, so either one can verify the other.
        """
        self.secret = self.config.secret_key
        if not self.secret:
            raise TrixError(
                f"{self.algorithm} signs with a shared secret and none was found: set "
                f"[TOKENS] SECRET_KEY in trix.ini, or put it in {self.config.secret_file}")
        if len(self.secret) < MINIMUM_SECRET_LENGTH:
            LOGGER.warning("the shared secret is %d characters long; the installer "
                           "writes %d", len(self.secret), MINIMUM_SECRET_LENGTH)
        LOGGER.info("signing as %s with %s and the secret shared with Luna",
                    self.issuer, self.algorithm)
        return self

    def _load_keypair(self) -> "KeyRing":
        """
        Read the private key and every public key in the trust directory.
        """
        self.private = read_private(self.config.private_key)
        self.trusted = read_trust(self.config.trust)
        self.trusted[self.key_id] = self.private.public_key()
        LOGGER.info("signing as %s with %s key %s, trusting %s", self.issuer,
                    self.algorithm, self.key_id,
                    ", ".join(sorted(self.trusted)) or "nothing else")
        return self

    def signing_key(self):
        """
        What this daemon mints with, or None when it holds nothing to mint with.
        """
        return (self.secret or None) if self.shared else self.private

    def verifying_key(self, key_id: str = ""):
        """
        What a presented token is checked against, or None when nothing trusts it.
        A shared secret has one answer and no key id, as Luna's own tokens carry.
        """
        if self.shared:
            return self.secret or None
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
