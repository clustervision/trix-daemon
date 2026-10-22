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
The published API: the document, what it says about access and errors, and the page that
shows it, with Swagger UI served from the package so it works without internet.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, DAEMON_VERSION

LOGGER = trixlog.get("docs")

TITLE: str = "TrinityX Daemon (trixd)"
SWAGGER_UI: str = "5.32.15"
STATIC_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "static")
STATIC_URL: str = f"{API_PREFIX}/docs/static"
OPENAPI_URL: str = f"{API_PREFIX}/openapi.json"
LICENSE_URL: str = f"{STATIC_URL}/LICENSE.txt"
# A plugin may name its own service in the description: it registers the placeholder
# it writes there and what to put in its place. The published document cannot know the
# reader's host, so each one offers a static stand-in for that copy too.
_placeholders: dict = {}


def register_placeholder(name: str, resolve, published: str) -> None:
    """
    Offer "{name}" in the description, resolved per request and published as published.
    """
    _placeholders[name] = (resolve, published)


def substitute(text: str, request: Request = None) -> str:
    """
    Replace every registered placeholder, with the reader's own address when there is a
    request and the static stand-in when there is not. A placeholder nothing registered is
    left as written, as `{name}`.

    Nothing takes a registration back, so an unplugged plugin's placeholder goes on being
    resolved: there is no forget here and unload calls none. Written down rather than
    fixed, because the one placeholder there is resolves to Luna's own address, where Luna
    answers whether or not this daemon forwards to it, and the paragraph naming it is
    stale once that route is gone however the placeholder reads.
    """
    for name, (resolve, published) in _placeholders.items():
        try:
            value = resolve(request) if request is not None else published
        except Exception as error:
            LOGGER.error("could not resolve the %s address for the document: %s", name, error)
            value = published
        text = text.replace("{" + name + "}", value)
    return text

DESCRIPTION: str = """\
**trixd** is the TrinityX daemon for everything that is not a physical node: the \
cluster's users and groups, the Prometheus monitoring configuration, starting and \
stopping services, and the slurm and genders files computed from Luna's node list. \
Every route below says what it does, what to send, what comes back, and who may call it.

**Luna**, luna2-daemon, runs beside it on this controller and owns the physical estate: \
nodes, groups, images, networks and boot. It answers at {luna}, and \
[{luna}/version]({luna}/version) says which version it runs. A request to \
`/api/v1/luna/<path>` here is handed to Luna as `/<path>`, unchanged, and Luna decides \
who may.

### Signing in
1. `POST /api/v1/auth/token` with `{"username": "...", "password": "..."}` answers \
`201` and `{"token": "..."}`, valid for an hour unless `trixd.ini` says otherwise.
2. Send the token on every call as `x-access-tokens: <token>`, as Luna takes it, or as \
`Authorization: Bearer <token>`.
3. On this page: **Authorize**, paste the token into either field, and every \
**Try it out** sends it.

### Who may call what
A user holds one role: `none` < `reader` < `operator` < `manager`. Each route admits a \
lowest role, shown as **Access** under its description. **rootus**, the account \
`trixd.ini` names, is the administrator and passes every check. `none` may only ask who \
it is, with `GET /api/v1/auth/whoami`.

### Ports
**7070** speaks the protocol `trixd.ini` names, https or http. **7071** is plain \
HTTP on every address, for nodes; a password or token sent there crosses the network in \
clear text.

### Errors
A refusal or a failure answers its status and `{"message": "..."}` saying why: 400 a \
request that cannot be acted on, 401 no token or a bad one, 403 a role below the \
route's minimum, 404 no such object, 409 a conflict, 413 a body larger than `[API] \
MAX_BODY`, 422 a body that does not match its schema (FastAPI's `detail` list instead of \
`message`), 502 the service behind trixd failed, 503 not available on this controller.
"""

ACCESS: dict = {
    "none": "any signed-in user",
    "reader": "reader or above: reader, operator, manager and rootus",
    "operator": "operator or above: operator, manager and rootus",
    "manager": "manager or above: manager and rootus",
    "rootus": "rootus only: the account trixd.ini names, and the other controller of a pair",
}
ERROR_SCHEMA: dict = {"title": "Error", "type": "object", "required": ["message"],
                      "properties": {"message": {"type": "string"}}}
REFUSALS: dict = {
    "role": {"401": ("No token, or one that is not valid", "a valid token is missing"),
             "403": ("The caller's role is below this route's minimum",
                     "this needs manager; you hold reader")},
    "node": {"401": ("No token, or one that is not valid", "a valid token is missing"),
             "403": ("Not a node token, or a token for another node",
                     "token is not valid for this node")},
}

# Swagger UI's own settings. validatorUrl None: it would otherwise send the document's
# address to validator.swagger.io. The Access line says what the raw extensions would.
SWAGGER_PARAMETERS: dict = {"validatorUrl": None, "showExtensions": False,
                            "displayRequestDuration": True, "docExpansion": "list",
                            "defaultModelsExpandDepth": 0}

router = APIRouter(prefix=API_PREFIX)


@router.get("/docs", include_in_schema=False)
def page() -> HTMLResponse:
    """
    FastAPI's Swagger page, on files from the package; trixd.css carries the TrinityX look.
    """
    return get_swagger_ui_html(
        openapi_url=OPENAPI_URL, title=TITLE,
        swagger_js_url=f"{STATIC_URL}/vendor/swagger-ui/swagger-ui-bundle.js?v={SWAGGER_UI}",
        swagger_css_url=f"{STATIC_URL}/trixd.css?v={DAEMON_VERSION}",
        swagger_favicon_url=f"{STATIC_URL}/favicon.ico",
        swagger_ui_parameters=SWAGGER_PARAMETERS)


@router.get("/openapi.json", include_in_schema=False)
def document(request: Request) -> JSONResponse:
    """
    The document, with Luna's address and the licence as this reader reaches them.
    """
    built = request.app.openapi()
    info = dict(built["info"])
    info["description"] = substitute(info.get("description", ""), request)
    info["license"] = {**info.get("license", {}),
                       "url": str(request.base_url).rstrip("/") + LICENSE_URL}
    return JSONResponse({**built, "info": info})


def answers(example=None, description: str = "", errors: dict = None,
            status: int = 200) -> dict:
    """
    A core route's responses for the document: its success with an example, and the
    refusals its own work can answer. The gate's 401 and 403 are added by finish().
    """
    found = {}
    if example is not None:
        found[status] = {"description": description or "Successful Response",
                         "content": {"application/json": {"example": example}}}
    for code, meaning in (errors or {}).items():
        found[code] = {"description": meaning, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Error"}}}}
    return found


def example(value) -> dict:
    """
    One example for a parameter or a body, in the shape Swagger UI lists.
    """
    return {"example": {"value": value}}


def access_line(operation: dict) -> str:
    """
    Who may call an operation, in words, from what the gate stamped on it.
    """
    access = operation.get("x-trixd-access", "")
    if access == "public":
        return "public, no token"
    if access == "node":
        return ("a node token for the node in the path, from "
                "`POST /api/v1/auth/node/{node}`")
    if access == "forward":
        return "decided by Luna: send a token Luna accepts, as you would to Luna"
    return ACCESS.get(operation.get("x-trixd-minimum", ""), "")


def finish(document: dict) -> dict:
    """
    What every operation says beyond its own declaration: who may call it, the refusals
    its gate can answer, and the token a forwarded call carries on.
    """
    # FastAPI takes only an absolute licence URL; served copies get this host's in front.
    document["info"].setdefault("license", {})["url"] = LICENSE_URL
    document.setdefault("components", {}).setdefault("schemas", {})["Error"] = ERROR_SCHEMA
    error = {"$ref": "#/components/schemas/Error"}
    for operations in document.get("paths", {}).values():
        for operation in operations.values():
            line = access_line(operation)
            if line:
                text = operation.get("description", "").rstrip()
                operation["description"] = (f"{text}\n\n**Access:** {line}" if text
                                            else f"**Access:** {line}")
            responses = operation.setdefault("responses", {})
            for status, (meaning, message) in REFUSALS.get(
                    operation.get("x-trixd-access", ""), {}).items():
                responses.setdefault(status, {
                    "description": meaning,
                    "content": {"application/json": {"schema": error,
                                                     "example": {"message": message}}}})
            if operation.get("x-trixd-access") == "forward":
                operation["security"] = [{"token": []}, {"bearer": []}]
    return document


def install(app: FastAPI) -> None:
    """
    Serve the page, its files and the document, and finish the document once per build.
    """
    app.mount(STATIC_URL, StaticFiles(directory=STATIC_DIR), name="docs-static")
    app.include_router(router)
    build = app.openapi

    def openapi() -> dict:
        """
        FastAPI's document, finished the first time after each build.
        """
        fresh = app.openapi_schema is None
        built = build()
        return finish(built) if fresh else built

    app.openapi = openapi
