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
The published API and the page that shows it: offline, in the TrinityX look, with every
route saying what it does and who may call it.
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
import re

import pytest

from trixd.common.errors import PluginRefused
from trixd.core.plugins.manifest import parse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC = "/api/v1/docs/static"


def _published() -> dict:
    """
    docs/openapi.json, as committed.
    """
    with open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _operations(document: dict) -> dict:
    """
    Every operation, keyed as VERB /path.
    """
    return {f"{verb.upper()} {path}": operation
            for path, verbs in document["paths"].items() for verb, operation in verbs.items()}


def test_the_page_needs_no_internet(client):
    """
    FastAPI's Swagger page, pointed at files the daemon serves, never at a CDN, and with
    Swagger's online validator off, so nothing leaves for validator.swagger.io.
    """
    answer = client.get("/api/v1/docs")
    assert answer.status_code == 200
    assert "<title>TrinityX Daemon (trixd)</title>" in answer.text
    assert not re.search(r'(src|href)="https?://', answer.text), "an asset from outside"
    for asset in ("vendor/swagger-ui/swagger-ui-bundle.js", "trixd.css", "favicon.ico"):
        assert f"{STATIC}/{asset}" in answer.text, asset
    assert '"validatorUrl": null' in answer.text
    assert '"showExtensions": false' in answer.text


def test_the_stylesheet_brings_swagger_and_the_logo():
    """
    trixd.css imports Swagger UI's own stylesheet first and draws the bar with the logo.
    """
    with open(os.path.join(ROOT, "trixd", "static", "trixd.css"), encoding="utf-8") as handle:
        css = handle.read()
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S).strip()
    assert rules.startswith('@import url("vendor/swagger-ui/swagger-ui.css'), rules[:80]
    assert 'url("logo.png")' in css and "TrinityX Daemon (trixd)" in css


@pytest.mark.parametrize("asset, kind", [
    ("vendor/swagger-ui/swagger-ui-bundle.js", "javascript"),
    ("vendor/swagger-ui/swagger-ui.css", "text/css"),
    ("trixd.css", "text/css"),
    ("logo.png", "image/png"),
    ("favicon.ico", "icon"),
    ("LICENSE.txt", "text/plain"),
])
def test_every_file_the_page_loads_is_served(client, asset, kind):
    """
    Each file the page names answers from the package.
    """
    answer = client.get(f"{STATIC}/{asset}")
    assert answer.status_code == 200, asset
    assert kind in answer.headers["content-type"], answer.headers["content-type"]
    assert answer.content


def test_the_licence_is_the_one_the_repository_carries(client):
    """
    The licence link opens our LICENSE.txt, shipped with the page's own files.
    """
    with open(os.path.join(ROOT, "LICENSE.txt"), encoding="utf-8") as handle:
        licence = handle.read()
    with open(os.path.join(ROOT, "trixd", "static", "LICENSE.txt"), encoding="utf-8") as handle:
        assert handle.read() == licence, "trixd/static/LICENSE.txt is a copy of LICENSE.txt"
    answer = client.get(f"{STATIC}/LICENSE.txt")
    assert answer.status_code == 200
    assert answer.text.replace("\r\n", "\n") == licence.replace("\r\n", "\n")


@pytest.mark.parametrize("image", ["logo.png", "favicon.ico"])
def test_an_image_is_served_byte_for_byte(client, image):
    """
    Images are bytes: nothing on the way may treat them as text and change line endings.
    """
    with open(os.path.join(ROOT, "trixd", "static", image), "rb") as handle:
        shipped = handle.read()
    assert client.get(f"{STATIC}/{image}").content == shipped
    if image.endswith(".png"):
        assert shipped.startswith(b"\x89PNG\r\n\x1a\n"), "the PNG signature"


def test_the_document_names_luna_on_the_host_the_reader_used(client):
    """
    Luna runs on the same controller, so its link uses the reader's host with luna.ini's
    protocol and port: the name luna.ini gives Luna may not resolve where the reader sits.
    """
    info = client.get("/api/v1/openapi.json").json()["info"]
    assert info["title"] == "TrinityX Daemon (trixd)"
    link = "https://testserver:7050/version"
    assert f"[{link}]({link})" in info["description"]
    assert "luna.test" not in info["description"]
    assert info["license"] == {"name": "GPL-3.0-or-later",
                               "url": "http://testserver/api/v1/docs/static/LICENSE.txt"}


def test_the_published_document_names_no_controller():
    """
    docs/openapi.json is the same everywhere, so it shows where Luna is, not one machine.
    """
    info = _published()["info"]
    assert "https://<controller>:7050/version" in info["description"]
    assert info["license"]["url"] == "/api/v1/docs/static/LICENSE.txt"


def test_one_authorize_for_both_token_headers():
    """
    The token is a security scheme, offered once, not a header field on every route.
    """
    document = _published()
    assert document["components"]["securitySchemes"]["token"]["name"] == "x-access-tokens"
    assert document["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    for name, operation in _operations(document).items():
        headers = [item["name"] for item in operation.get("parameters", [])
                   if item["in"] == "header"]
        assert not headers, (name, headers)
        if operation["x-trixd-access"] in ("role", "node", "forward"):
            assert operation["security"] == [{"token": []}, {"bearer": []}], name


@pytest.mark.parametrize("how", ["x-access-tokens", "bearer"])
def test_both_headers_still_carry_the_token(client, token, how):
    """
    The schemes change what the document shows, not what the gate accepts.
    """
    sent = ({"Authorization": f"bearer {token}"} if how == "bearer"
            else {"x-access-tokens": token})
    assert client.get("/api/v1/system/plugins", headers=sent).status_code == 200
    assert client.get("/api/v1/system/plugins").status_code == 401


def test_every_route_says_who_may_call_it_in_words():
    """
    Beside the stamped minimum, each description ends with who may call it, and a route
    behind the gate lists the refusals the gate answers.
    """
    for name, operation in _operations(_published()).items():
        assert "**Access:** " in operation.get("description", ""), name
        if operation["x-trixd-access"] in ("role", "node"):
            assert {"401", "403"} <= set(operation["responses"]), name


@pytest.mark.parametrize("name", sorted(_operations(_published())))
def test_every_route_says_what_to_send_and_what_comes_back(name):
    """
    Every route, documented in full: what it does, each parameter explained with an
    example, an example body, and an example answer.
    """
    operation = _operations(_published())[name]
    assert len(operation.get("description", "")) > 2 * len(operation["summary"]), name
    for parameter in operation.get("parameters", []):
        assert parameter.get("description"), (name, parameter["name"])
        assert parameter.get("examples"), (name, parameter["name"])
    if "requestBody" in operation:
        assert operation["requestBody"]["content"]["application/json"].get("examples"), name
    success = next((code for code in ("200", "201", "202")
                    if code in operation["responses"]), "")
    assert success, f"{name} documents no success answer"
    content = operation["responses"][success].get("content", {})
    assert any(item.get("example") is not None for item in content.values()), name


def test_the_query_a_handler_reads_is_declared():
    """
    ?nodes and ?detail were read inside the handler and invisible; now the document has them.
    """
    operation = _operations(_published())["GET /api/v1/prometheus/hw-rules"]
    assert {(item["name"], item["in"]) for item in operation["parameters"]} == {
        ("nodes", "query"), ("detail", "query")}


@pytest.mark.parametrize("endpoint, reason", [
    ({"params": {"host": {"description": "x"}}}, "which its path does not have"),
    ({"query": {"body": {"description": "x"}}}, "which it cannot take"),
    ({"query": {"since": {"type": "date"}}}, "one of string, integer, boolean"),
    ({"errors": {302: "moved"}}, "400 to 599 only"),
])
def test_documentation_that_could_not_be_true_is_refused(endpoint, reason):
    """
    A manifest cannot document a path variable it lacks, a query name the binder uses
    itself, a query type it cannot check, or a success as an error.
    """
    document = {"plugin": "docs-test", "endpoints": [
        {"path": "items/<name>", "minimum": "reader", "handler": "get", **endpoint}]}
    with pytest.raises(PluginRefused, match=reason):
        parse(document)


def test_a_declared_query_parameter_reaches_a_handler_that_takes_it():
    """
    The page shows it, FastAPI checks its type, and the handler receives it as an argument.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from trixd.core.plugins import binder

    class Backend:
        """
        Answers with what it was given.
        """

        def count(self, name: str, limit: int) -> dict:
            """
            Echo the path variable and the query parameter.
            """
            return {"name": name, "limit": limit}

    manifest = parse({"plugin": "docs-test", "endpoints": [
        {"path": "items/<name>", "public": True, "handler": "count",
         "query": {"limit": {"type": "integer", "description": "how many", "example": 5}}}]})
    app = FastAPI()
    app.include_router(binder.build(manifest, Backend(), {}))
    client = TestClient(app)
    assert client.get("/api/v1/docs-test/items/a?limit=3").json() == {"name": "a", "limit": 3}
    assert client.get("/api/v1/docs-test/items/a?limit=many").status_code == 422
    query = app.openapi()["paths"]["/api/v1/docs-test/items/{name}"]["get"]["parameters"][1]
    assert (query["name"], query["in"], query["description"]) == ("limit", "query", "how many")


def test_a_body_field_carries_its_description_and_example():
    """
    Field docs in schemas reach the published model, so Try it out starts from an example.
    """
    force = _published()["components"]["schemas"]["PrometheusHw_Rules"]["properties"]["force"]
    assert force["description"].startswith("Generate the file afresh")
    assert force["examples"] == [False]
