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
The daemon the end-to-end suite talks to: a real trixd process started here against the
stand-in Luna (TRIXD_E2E=local), or one already running on a controller
(TRIXD_E2E_URL=http://<host>:8051). Either way the suite only speaks HTTP to it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import socket
import subprocess
import sys
import tempfile
import time
from configparser import RawConfigParser
from datetime import datetime, timedelta, timezone

import jwt
import requests
import urllib3

from tests.e2e.fakeluna import FakeLuna

urllib3.disable_warnings()

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFIX = "/api/v1"
LUNA_INI = "/trinity/local/luna/daemon/config/luna.ini"
TRIXD_INI = "/trinity/local/trixd/daemon/config/trixd.ini"
# Every name the suite creates starts with this, so it can find and remove all of them.
MARK = "e2e-"
ROLES = ("none", "reader", "operator", "manager")
RANK = {"none": 0, "reader": 1, "operator": 2, "manager": 3, "rootus": 4}


def _free_port() -> int:
    """
    A port nothing listens on, for a moment.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Stack:
    """
    One daemon, how to reach it, and as whom.
    """

    def __init__(self) -> None:
        """
        Nothing started yet.
        """
        self.local = False
        self.base = ""
        self.user = ""
        self.password = ""
        self.secret = ""
        self.luna = None
        self.process = None
        self.scratch = None
        self.session = requests.Session()
        self.tokens: dict = {}
        self.nodes: list = []

    # ------------------------------------------------------------------ start

    def start(self) -> "Stack":
        """
        Remote when TRIXD_E2E_URL names a daemon, local otherwise.
        """
        url = os.environ.get("TRIXD_E2E_URL", "")
        if url:
            self.base = url.rstrip("/") + PREFIX
            self.user = os.environ.get("TRIXD_E2E_USER", "trixd")
            self.password = os.environ.get("TRIXD_E2E_PASSWORD", "trixd")
            self.secret = os.environ.get("TRIXD_E2E_SECRET", "") or self._secret_from_ini()
        else:
            self._start_local()
        self._wait()
        self.nodes = self._luna_nodes()
        return self

    def _secret_from_ini(self) -> str:
        """
        The secret the daemon signs with, when this runs on the controller and may read
        its ini. trixd's own comes first; a controller where only Luna's is set still
        works, because the installer writes the same secret to both.
        """
        for path, section in ((os.environ.get("TRIXD_INI", TRIXD_INI), "TOKENS"),
                              (os.environ.get("TRIXD_LUNA_INI", LUNA_INI), "API")):
            parser = RawConfigParser()
            try:
                parser.read(path)
                found = parser.get(section, "SECRET_KEY", fallback="").strip()
            except Exception:
                found = ""
            if found:
                return found
        return ""

    def _start_local(self) -> None:
        """
        The stand-in Luna, then trixd as its own process, plain HTTP on the loopback.
        """
        self.local = True
        self.scratch = tempfile.mkdtemp(prefix="trixd-e2e-")
        self.secret = "e2e" + "0" * 29
        self.luna = FakeLuna(self.secret, nodes=3)
        luna_port = self.luna.start()
        luna_ini = os.path.join(self.scratch, "luna.ini")
        with open(luna_ini, "w", encoding="utf-8") as handle:
            handle.write(f"[API]\nENDPOINT = 127.0.0.1:{luna_port}\nPROTOCOL = http\n"
                         f"VERIFY_CERTIFICATE = no\nSECRET_KEY = {self.secret}\n")
        port = _free_port()
        self.user, self.password = "e2e-root", "e2e-root-password"
        ini = os.path.join(self.scratch, "trixd.ini")
        database = os.path.join(self.scratch, "trixd.db").replace(os.sep, "/")
        with open(ini, "w", encoding="utf-8") as handle:
            handle.write(
                f"[LOGGER]\nLEVEL = info\nLOGFILE = {os.path.join(self.scratch, 'trixd.log')}\n"
                f"[API]\nBIND =\nINSECURE_BIND = 127.0.0.1:{port}\n"
                f"USERNAME = {self.user}\nPASSWORD = {self.password}\nEXPIRY = 1h\n"
                f"[TOKENS]\nSECRET_KEY = {self.secret}\n"
                f"[DATABASE]\nURL = sqlite:///{database}\n"
                f"[AUTH]\nCHAIN = local\nCACHE = 0s\n"
                f"[SERVICES]\nCOMMAND = {sys.executable}\n"
                f"[PLUGINS]\nDIRECTORY = {os.path.join(ROOT, 'trixd', 'plugins')}\n"
                f"SITE_DIRECTORY = {os.path.join(self.scratch, 'site')}\nENABLED =\n")
        environment = dict(os.environ, TRIXD_LUNA_INI=luna_ini, PYTHONPATH=ROOT)
        self.log = open(os.path.join(self.scratch, "stdout.log"), "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "trixd.server", "--config", ini], cwd=ROOT,
            env=environment, stdout=self.log, stderr=subprocess.STDOUT)
        self.base = f"http://127.0.0.1:{port}{PREFIX}"

    def _wait(self) -> None:
        """
        Until /system/health answers, or say why it never did.
        """
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"trixd exited with {self.process.returncode}; see "
                                   f"{self.scratch}")
            try:
                if self.session.get(f"{self.base}/system/health", timeout=2,
                                    verify=False).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError(f"trixd at {self.base} did not answer within 60s")
        if self.local:
            rules = os.path.join(self.scratch, "rules")
            os.makedirs(rules, exist_ok=True)
            answer = self.call("PATCH", "/prometheus/config", self.rootus(), json={
                "rules_directory": rules + os.sep,
                "prometheus_url": f"http://127.0.0.1:{self.luna.server.server_address[1]}",
                "reload_command": f"{sys.executable} -c pass"})
            assert answer.status_code == 200, answer.text

    def _luna_nodes(self) -> list:
        """
        The nodes Luna holds, through the pass-through, for routes that name a node.
        """
        answer = self.call("GET", "/luna/config/node", self.rootus())
        if answer.status_code != 200:
            return []
        return sorted((answer.json().get("config") or {}).get("node") or {})

    def stop(self) -> None:
        """
        Remove what the suite created, then stop what it started.
        """
        try:
            listed = self.call("GET", "/identity/users", self.rootus())
            for user in (listed.json().get("users") or []) if listed.status_code == 200 else []:
                name = user.get("name", "") if isinstance(user, dict) else str(user)
                if name.startswith(MARK):
                    self.call("DELETE", f"/identity/users/{name}", self.rootus())
        except Exception:
            pass
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.log.close()
        if self.luna is not None:
            self.luna.stop()

    # ------------------------------------------------------------------ calls

    def call(self, method: str, path: str, token: str = None, headers: dict = None,
             **kwargs) -> requests.Response:
        """
        One request under /api/v1, with a token when one is given.
        """
        sent = dict(headers or {})
        if token:
            sent["x-access-tokens"] = token
        try:
            return self.session.request(method, f"{self.base}{path}", headers=sent,
                                        timeout=60, verify=False, **kwargs)
        except requests.ConnectionError:
            # A reused connection the server had just closed; once more on a fresh one.
            return self.session.request(method, f"{self.base}{path}", headers=sent,
                                        timeout=60, verify=False, **kwargs)

    def sign_in(self, name: str, password: str) -> requests.Response:
        """
        POST /auth/token.
        """
        return self.call("POST", "/auth/token", json={"username": name, "password": password})

    def rootus(self) -> str:
        """
        A token for the ini account, fetched once.
        """
        if "rootus" not in self.tokens:
            answer = self.sign_in(self.user, self.password)
            assert answer.status_code == 201, f"rootus cannot sign in: {answer.text}"
            self.tokens["rootus"] = answer.json()["token"]
        return self.tokens["rootus"]

    def as_role(self, role: str) -> str:
        """
        A token for a user holding this role, created once per run.
        """
        if role == "rootus":
            return self.rootus()
        if role not in self.tokens:
            name = f"{MARK}{role}"
            body = {"password": f"{name}-password"}
            if role != "none":
                body["role"] = role
            made = self.call("PUT", f"/identity/users/{name}", self.rootus(), json=body)
            assert made.status_code == 200, made.text
            answer = self.sign_in(name, f"{name}-password")
            assert answer.status_code == 201, answer.text
            self.tokens[role] = answer.json()["token"]
        return self.tokens[role]

    def node_token(self, node: str) -> str:
        """
        A node token, on a credential, as a node gets one where security is off.
        """
        answer = self.call("POST", f"/auth/node/{node}",
                           json={"username": self.user, "password": self.password})
        assert answer.status_code == 201, answer.text
        return answer.json()["token"]

    def mint(self, subject: str, expiry: int = 300, secret: str = "", **claims) -> str:
        """
        A token signed here, for cases the daemon never issues: expired, foreign.
        """
        issued = datetime.now(timezone.utc)
        payload = {"iss": "trixd", "sub": subject, "kind": "user", "src": "local",
                   "iat": issued, "exp": issued + timedelta(seconds=expiry), "id": -1}
        payload.update(claims)
        return jwt.encode(payload, secret or self.secret, algorithm="HS256")

    def document(self) -> dict:
        """
        The live OpenAPI document.
        """
        return self.call("GET", "/openapi.json").json()


def operations(document: dict) -> list:
    """
    Every operation in a document as (verb, path, operation), in a stable order.
    """
    found = []
    for path, verbs in sorted(document["paths"].items()):
        for verb, operation in sorted(verbs.items()):
            found.append((verb.upper(), path[len(PREFIX):], operation))
    return found


def fill(path: str, value: str = f"{MARK}x") -> str:
    """
    A concrete path for a template: every {variable} gets the value.
    """
    out = []
    for part in path.split("/"):
        out.append(value if part.startswith("{") and part.endswith("}") else part)
    return "/".join(out)
