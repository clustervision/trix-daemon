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
A stand-in for Luna and for Prometheus on one port, so the end-to-end suite runs where
there is no controller. It checks tokens as Luna does: id 0 is its administrator, id -1
is nobody, and a provision token is confined to its own node.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import jwt

HOST_IN_QUERY = re.compile(r'hostname="([^"]+)"')


class FakeLuna:
    """
    Nodes, groups, a cluster row and table hashes, as Luna answers them, plus the one
    Prometheus query the hardware rules ask.
    """

    def __init__(self, secret: str, nodes: int = 3) -> None:
        """
        The secret it verifies with, and a few nodes in one group.
        """
        self.secret = secret
        self.lock = threading.Lock()
        self.nodes = {}
        for number in range(1, nodes + 1):
            name = f"node{number:03d}"
            self.nodes[name] = {"name": name, "hostname": f"{name}.cluster", "group": "compute",
                                "provision_interface": "BOOTIF",
                                "interfaces": [{"interface": "BOOTIF",
                                                "ipaddress": f"10.141.0.{number}"}]}
        self.calls = []
        self.server = None

    def start(self) -> int:
        """
        Serve on a free port of the loopback, in a thread; answer the port.
        """
        fake = self

        class Handler(BaseHTTPRequestHandler):
            """
            One request at a time per thread.
            """

            def log_message(self, *args) -> None:
                """
                Quiet: the suite reads answers, not a log.
                """

            def _answer(self, status: int, body=None) -> None:
                """
                Send JSON, or nothing for 204.
                """
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _who(self):
                """
                The token's claims, or None when it does not verify.
                """
                token = self.headers.get("x-access-tokens", "")
                try:
                    return jwt.decode(token, fake.secret, algorithms=["HS256"])
                except jwt.PyJWTError:
                    return None

            def _route(self, method: str) -> None:
                """
                Answer as Luna, or as Prometheus for /api/v1/query.
                """
                parsed = urlparse(self.path)
                path = parsed.path.strip("/")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with fake.lock:
                    fake.calls.append((method, path))
                if path == "api/v1/query":
                    query = parse_qs(parsed.query).get("query", [""])[0]
                    found = HOST_IN_QUERY.search(query)
                    host = found.group(1) if found else ""
                    result = [{"metric": {"hostname": host, "luna_group": "compute",
                                          "class": "memory", "description": "DIMM",
                                          "product": "P", "vendor": "V", "serial": "S1"}}]
                    self._answer(200, {"status": "success",
                                       "data": {"resultType": "vector", "result": result}})
                    return
                claims = self._who()
                if claims is None:
                    self._answer(401, {"message": "token is invalid"})
                    return
                if claims.get("scope") == "provision":
                    own = f"config/node/{claims.get('node', '')}"
                    if method != "GET" or path != own:
                        self._answer(401, {"message": "a provision token is for its node"})
                        return
                elif claims.get("id", 0) != 0:
                    self._answer(401, {"message": f"user {claims.get('id')} no longer exists"})
                    return
                self._luna(method, path, body)

            def _luna(self, method: str, path: str, body: bytes) -> None:
                """
                The few Luna routes trixd reads, and a rename and a delete.
                """
                parts = path.split("/")
                with fake.lock:
                    nodes = dict(fake.nodes)
                if method == "GET" and path == "config/node":
                    self._answer(200, {"config": {"node": nodes}})
                elif method == "GET" and len(parts) == 3 and parts[:2] == ["config", "node"]:
                    if parts[2] in nodes:
                        self._answer(200, {"config": {"node": {parts[2]: nodes[parts[2]]}}})
                    else:
                        self._answer(404, {"message": f"no node {parts[2]}"})
                elif method == "GET" and len(parts) == 4 and parts[3] == "_delete":
                    with fake.lock:
                        removed = fake.nodes.pop(parts[2], None)
                    self._answer(204 if removed else 404)
                elif method == "POST" and len(parts) == 3 and parts[:2] == ["config", "node"]:
                    try:
                        data = json.loads(body or b"{}")["config"]["node"][parts[2]]
                    except (ValueError, KeyError, TypeError):
                        data = {}
                    with fake.lock:
                        record = fake.nodes.get(parts[2])
                        if record is None:
                            self._answer(404, {"message": f"no node {parts[2]}"})
                            return
                        if data.get("group"):
                            record["group"] = data["group"]
                        if data.get("newnodename"):
                            new = data["newnodename"]
                            record["name"] = new
                            record["hostname"] = f"{new}.cluster"
                            fake.nodes[new] = fake.nodes.pop(parts[2])
                    self._answer(204)
                elif method == "GET" and path == "config/group":
                    self._answer(200, {"config": {"group": {"compute": {"name": "compute"}}}})
                elif method == "GET" and path == "config/network":
                    self._answer(200, {"config": {"network": {"cluster": {"name": "cluster"}}}})
                elif method == "GET" and path == "config/cluster":
                    self._answer(200, {"config": {"cluster": {"security": False}}})
                elif method == "GET" and path == "table/hashes":
                    self._answer(200, {"table": {"hashes": {"node": str(len(nodes))}}})
                else:
                    self._answer(404, {"message": f"{method} {path} is not something this Luna does"})

            def do_GET(self) -> None:
                """
                GET.
                """
                self._route("GET")

            def do_POST(self) -> None:
                """
                POST.
                """
                self._route("POST")

            def do_PATCH(self) -> None:
                """
                PATCH.
                """
                self._route("PATCH")

            def do_DELETE(self) -> None:
                """
                DELETE.
                """
                self._route("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="fake-luna", daemon=True).start()
        return self.server.server_address[1]

    def stop(self) -> None:
        """
        Stop serving.
        """
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
