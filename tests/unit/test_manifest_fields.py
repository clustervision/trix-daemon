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
Every field a manifest may carry does something: a changed value moves what the daemon
serves, or the manifest is refused. And what is accepted in silence is said or refused.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import copy
import glob
import logging
import os
import re

import pytest
import yaml
from starlette.testclient import TestClient

import trixd
from trixd.api.v1 import system
from trixd.common.constants import (API_PREFIX, HTTP_VERBS, MINIMUMS, PLUGIN_NAME_PATTERN,
                                    TOKEN_KIND_NODE, TOKEN_KIND_USER)
from trixd.common.errors import PluginRefused
from trixd.common.settings import current as settings, seconds
from trixd.core.plugins import binder, loader, manifest as manifests, scaffold
from trixd.core.plugins.check import check
from trixd.core.plugins.registry import current as registry
from trixd.core.plugins.schema import manifest_schema
from trixd.core.render.engine import current as renders

SHIPPED = sorted(os.path.dirname(path) for path in glob.glob(
    os.path.join(os.path.dirname(trixd.__file__), "plugins", "*", "manifest.yaml")))


class Said(logging.Handler):
    """
    Every record a logger emits, kept for a test to read.
    """

    def __init__(self) -> None:
        """
        An empty list of records.
        """
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record) -> None:
        """
        Keep the record.
        """
        self.records.append(record)

    def warnings(self, about: str = "") -> list:
        """
        The warnings said, as text, that mention `about`.
        """
        return [record.getMessage() for record in self.records
                if record.levelno == logging.WARNING and about in record.getMessage()]


def _listen(logger) -> Said:
    """
    A handler on the module's own logger: one that is configured no longer propagates.
    """
    said = Said()
    logger.addHandler(said)
    return said


def _refusal(attempt) -> str:
    """
    What a load or a parse was refused with, or nothing when it went through.
    """
    try:
        attempt()
    except PluginRefused as refused:
        return refused.message
    return ""


def _write(name: str, document: dict, backend: str, other: str = "") -> str:
    """
    A custom plugin on disk, where the rig looks for one.
    """
    directory = os.path.join(settings().plugins.custom_directory, name)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "manifest.yaml"), "w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle)
    for module, source in (("backend", backend), ("other", other), ("__init__", "")):
        with open(os.path.join(directory, f"{module}.py"), "w", encoding="utf-8") as handle:
            handle.write(source)
    return directory


def _token(client) -> dict:
    """
    The ini account's token, as headers.
    """
    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "tester", "password": "secret"})
    assert answer.status_code == 201, answer.text
    return {"x-access-tokens": answer.json()["token"]}


KEEPER = (
    'class Backend:\n    """Answers with the body it was handed."""\n\n'
    '    def keep(self, node: str, body: dict = None) -> dict:\n'
    '        """What arrived."""\n'
    '        return {"node": node, "body": body}\n')

KEEPER_MANIFEST = {
    "plugin": "keeper", "backend": "backend",
    "schemas": {"shape": {"count": {"type": "integer"},
                          "note": {"type": "string", "optional": True}}},
    "endpoints": [{"path": "things/<node>", "methods": ["PATCH"], "minimum": "manager",
                   "handler": "keep", "schema": "shape", "replicate": True}]}


def test_a_field_the_schema_does_not_declare_is_named_and_never_handed_over(rig):
    """
    A body field no schema declares is dropped before the handler, as it always was; what
    is new is that the log says so, once for the request, with the route and the names.
    The values are never written: a body is where a password travels.
    """
    _write("keeper", KEEPER_MANIFEST, KEEPER)
    assert loader.load(rig, "keeper").state == "loaded"
    client = TestClient(rig)
    headers = _token(client)
    said = _listen(binder.LOGGER)
    try:
        plain = client.patch(f"{API_PREFIX}/keeper/things/node001", headers=headers,
                             json={"count": 3})
        answer = client.patch(f"{API_PREFIX}/keeper/things/node001", headers=headers,
                              json={"count": 3, "password": "hunter2", "colour": "blue"})
    finally:
        binder.LOGGER.removeHandler(said)

    # What the handler is handed does not change: the declared fields and nothing else.
    assert answer.status_code == 200, answer.text
    assert answer.json()["body"] == {"count": 3, "note": None}, answer.text
    assert plain.json()["body"] == {"count": 3, "note": None}, plain.text

    warnings = said.warnings("does not declare")
    assert len(warnings) == 1, warnings
    assert "PATCH keeper things/<node>" in warnings[0], warnings
    assert "'password'" in warnings[0] and "'colour'" in warnings[0], warnings
    assert "hunter2" not in warnings[0] and "blue" not in warnings[0], warnings


def test_the_names_a_caller_chose_are_quoted_and_bounded():
    """
    The names are the caller's, so the line that repeats them cannot be made to forge a
    second line or to grow with the body.
    """
    models = manifests.build_models(manifests.parse(KEEPER_MANIFEST))
    body = models["shape"](count=1, **{f"extra{number}": number for number in range(14)},
                           **{"two\nlines": 1, "x" * 500: 1})
    told = manifests.undeclared(body)
    assert body.model_dump() == {"count": 1, "note": None}, body.model_dump()
    assert told.count("'extra") == manifests.UNDECLARED_SHOWN, told
    assert told.endswith("and 6 more"), told

    body = models["shape"](count=1, **{"two\nlines": 1, "x" * 500: 1})
    told = manifests.undeclared(body)
    assert "\n" not in told and "two\\nlines" in told, told
    assert len(told) < 2 * manifests.UNDECLARED_NAME_LENGTH + 40, len(told)
    assert manifests.undeclared(models["shape"](count=1)) == ""
    assert manifests.undeclared(None) == "" and manifests.undeclared({"count": 1}) == ""


def test_a_replicated_call_with_an_undeclared_field_is_applied_and_named(rig):
    """
    The other controller applies through the same model, so it says the same and refuses
    nothing: a peer one release ahead sends a field this build does not know.
    """
    _write("keeper", KEEPER_MANIFEST, KEEPER)
    assert loader.load(rig, "keeper").state == "loaded"
    client = TestClient(rig)
    said = _listen(system.LOGGER)
    try:
        answer = client.post(
            f"{API_PREFIX}/system/replication/apply", headers=_token(client),
            json={"plugin": "keeper", "path": "things/<node>", "method": "PATCH",
                  "variables": {"node": "node001"}, "origin": "ctrl1", "request_id": "r-1",
                  "body": {"count": 3, "secret": "s3cr3t"}})
    finally:
        system.LOGGER.removeHandler(said)

    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["body"] == {"count": 3, "note": None}, answer.text
    warnings = said.warnings("does not declare")
    assert len(warnings) == 1, warnings
    assert "'secret'" in warnings[0] and "ctrl1" in warnings[0], warnings
    assert "PATCH keeper things/<node>" in warnings[0], warnings
    assert "s3cr3t" not in warnings[0], warnings


def _document(endpoint: dict, **beside) -> dict:
    """
    A manifest around one endpoint.
    """
    return {"plugin": "probe", "endpoints": [endpoint], **beside}


def _parse_refusal(endpoint: dict, **beside) -> str:
    """
    What the parse refuses a one-endpoint manifest with, or nothing.
    """
    return _refusal(lambda: manifests.parse(_document(endpoint, **beside)))


READ = {"path": "things/<name>", "methods": ["GET"], "minimum": "reader", "handler": "read"}
RENDER = {"path": "renders/thing", "methods": ["GET"], "minimum": "reader",
          "render": {"inputs": ["luna.node"], "serve": "json"}}


def test_a_path_variable_in_braces_is_refused_and_points_at_the_spelling():
    """
    The router matches {name} as a variable and the binder, which reads <name>, never
    hands it to the handler: a route that answers for every value and is told none.
    """
    assert _parse_refusal(READ) == ""
    for path in ("things/{name}", "things/{name", "things/name}"):
        refusal = _parse_refusal({**READ, "path": path})
        assert "<name>" in refusal and "brace" in refusal, (path, refusal)


def test_a_path_variable_documented_as_anything_but_a_string_is_refused():
    """
    A path variable reaches the handler as text whatever `params` says of it, so a type
    there was shown nowhere and checked by nothing.
    """
    assert _parse_refusal({**READ, "params": {"name": {"type": "string"}}}) == ""
    assert _parse_refusal({**READ, "params": {"name": {"description": "which"}}}) == ""
    refusal = _parse_refusal({**READ, "params": {"name": {"type": "integer"}}})
    assert "name as integer" in refusal and "always a string" in refusal, refusal


def test_a_render_serves_json_or_nothing():
    """
    Any word under `serve` was taken as yes and answered as JSON, so `serve: yaml` loaded
    and served something its manifest does not say.
    """
    sound = {"inputs": ["luna.node"]}
    assert _parse_refusal({**RENDER, "render": {**sound, "serve": "json"}}) == ""
    assert _parse_refusal({**RENDER, "render": {**sound, "serve": ""}}) == ""
    assert _parse_refusal({**RENDER, "render": sound}) == ""
    for word in ("yaml", "JSON", "text"):
        refusal = _parse_refusal({**RENDER, "render": {**sound, "serve": word}})
        assert f"serves {word}" in refusal, (word, refusal)


def test_a_render_that_serves_nothing_need_not_name_a_role():
    """
    No route is bound for a render with no `serve`, so no gate ever reads its minimum.
    One that is there is still held to the ladder, and a render that serves still needs it.
    """
    unserved = {"path": "renders/files", "methods": ["GET"],
                "render": {"inputs": ["luna.node"]}}
    assert _parse_refusal(unserved) == ""
    assert _parse_refusal({**unserved, "minimum": "reader"}) == ""
    assert "minimum role" in _parse_refusal({**unserved, "minimum": "everyone"})
    served = {**unserved, "render": {"inputs": ["luna.node"], "serve": "json"}}
    assert "minimum role" in _parse_refusal(served)
    assert "minimum role" in _parse_refusal({key: value for key, value in READ.items()
                                             if key != "minimum"})


@pytest.mark.parametrize("fields", [{}, None, []], ids=repr)
def test_a_schema_with_no_fields_is_refused(fields):
    """
    Its model takes any body at all and hands the handler an empty one.
    """
    write = {"path": "things", "methods": ["PATCH"], "minimum": "manager",
             "handler": "write", "schema": "change"}
    assert _parse_refusal(write, schemas={"change": {"value": {"type": "string"}}}) == ""
    refusal = _parse_refusal(write, schemas={"change": fields})
    assert "schema change declares no fields" in refusal, refusal
    # Whether or not a route uses it: it is what the manifest declares that is read.
    refusal = _parse_refusal(READ, schemas={"spare": fields})
    assert "schema spare declares no fields" in refusal, refusal


def _built(rule) -> dict:
    """
    The published form of a one-field schema, or the refusal as its only key.
    """
    try:
        manifest = manifests.parse(_document(READ, schemas={"change": {"value": rule}}))
    except PluginRefused as refused:
        return {"refused": refused.message}
    return manifests.build_models(manifest)["change"].model_json_schema()


def test_a_rule_says_only_what_a_request_model_honours():
    """
    An unknown type was checked as a string and an unknown key was dropped, both in
    silence: `type: int` accepted text, and `required: false` left the field required.
    """
    for rule in ({"type": "int"}, {"type": "array", "items": "strings"}, {"type": None}):
        refusal = _built(rule).get("refused", "")
        assert "schema change field value has" in refusal, (rule, _built(rule))
        assert "one of string, integer" in refusal, refusal
    refusal = _built({"type": "string", "required": False, "default": ""}).get("refused", "")
    assert "declares required, default, which is not one of type, items" in refusal, refusal
    assert "is not a rule" in _built("string").get("refused", "")

    # Every key and every type there is still builds, and so does a field with no rule.
    for name in manifests.TYPES:
        assert "refused" not in _built({"type": name}), name
        assert "refused" not in _built({"type": "array", "items": name}), name
    whole = _built({"type": "string", "items": "string", "optional": True,
                    "pattern": "^a", "description": "A value", "example": "a"})
    assert whole["properties"]["value"]["description"] == "A value", whole
    bare = _built(None)
    assert bare["properties"]["value"]["type"] == "string", bare
    assert bare["required"] == ["value"], bare


class Producers:
    """
    Producers of each shape a backend might offer the engine.
    """

    def render_plain(self) -> dict:
        """
        Called with nothing, as the engine calls it.
        """
        return {}

    def render_defaulted(self, force: bool = False, *more, **named) -> dict:
        """
        Still callable with nothing.
        """
        return {}

    def render_wanting(self, node: str) -> dict:
        """
        Needs an argument nothing supplies.
        """
        return {"node": node}

    async def render_awaited(self) -> dict:
        """
        Never awaited by the engine.
        """
        return {}


def test_a_render_producer_the_engine_could_not_call_is_refused():
    """
    The engine calls a producer with no arguments and keeps what it returns. One that
    needs an argument failed on every wake, and an async one handed back a coroutine that
    was stored as the document.
    """
    def producer(method: str) -> str:
        """
        The refusal for a render whose producer is this method.
        """
        endpoint = manifests.parse(_document({**RENDER, "handler": method})).endpoints[0]
        return _refusal(lambda: loader._render_producer(endpoint, Producers()))

    assert producer("render_plain") == ""
    assert producer("render_defaulted") == ""
    assert "takes arguments" in producer("render_wanting"), producer("render_wanting")
    assert "is async" in producer("render_awaited"), producer("render_awaited")


class Replicating:
    """
    Handlers of each shape a replicating route might name.
    """

    def sound(self, node: str, body: dict = None, caller=None, request=None) -> dict:
        """
        Everything the peer does not hand over has a default.
        """
        return {}

    def needs_caller(self, node: str, caller) -> dict:
        """
        The peer has no caller to give.
        """
        return {}

    def needs_request(self, node: str, request) -> dict:
        """
        Nor a request.
        """
        return {}

    def needs_body(self, node: str, body: dict) -> dict:
        """
        Handed a body only where a schema declares one.
        """
        return {}


@pytest.mark.parametrize("handler, schema, replicate, needs", [
    ("sound", "", True, ""),
    ("needs_caller", "", True, "caller"),
    ("needs_request", "", True, "request"),
    ("needs_body", "", True, "body"),
    ("needs_body", "change", True, ""),
    # The route itself hands all three over, so only a replicating one is held to this.
    ("needs_caller", "", False, ""),
    ("needs_body", "", False, ""),
])
def test_a_replicating_handler_asks_only_for_what_the_peer_hands_over(handler, schema,
                                                                    replicate, needs):
    """
    The other controller calls the handler with the path variables and a declared body.
    A handler needing more loads here, serves here, and answers 500 there on every
    delivery - which the sender reads as a peer to keep retrying.
    """
    endpoint = {"path": "things/<node>", "methods": ["PATCH"], "minimum": "manager",
                "handler": handler, "replicate": replicate}
    if schema:
        endpoint["schema"] = schema
    manifest = manifests.parse(_document(
        endpoint, schemas={"change": {"value": {"type": "string"}}}))
    problems = binder.endpoint_problems(manifest, Replicating(), manifest.endpoints[0])
    if not needs:
        assert problems == [], problems
        return
    assert len(problems) == 1, problems
    assert f"{handler} needs {needs}" in problems[0], problems
    assert "other controller" in problems[0], problems


def test_a_plugin_named_outside_the_published_pattern_loads_and_is_told(rig):
    """
    The schema an editor checks a manifest against asks for lower case, digits and dashes,
    and the loader never did. Such a plugin still loads; the log says what will flag it.
    """
    backend = 'class Backend:\n    """One route."""\n\n' \
              '    def show(self) -> dict:\n        """Nothing."""\n        return {}\n'
    endpoints = [{"path": "", "methods": ["GET"], "minimum": "reader", "handler": "show"}]
    said = _listen(loader.LOGGER)
    try:
        for name in ("well-named", "odd_name"):
            _write(name, {"plugin": name, "endpoints": endpoints}, backend)
            assert loader.load(rig, name).state == "loaded", name
    finally:
        loader.LOGGER.removeHandler(said)
    assert TestClient(rig).get(f"{API_PREFIX}/odd_name").status_code == 401

    assert said.warnings("well-named") == [], said.warnings("well-named")
    warnings = said.warnings("odd_name")
    assert len(warnings) == 1, warnings
    assert PLUGIN_NAME_PATTERN in warnings[0] and "loads" in warnings[0], warnings


def test_everything_shipped_still_loads_and_passes_the_new_checks(rig):
    """
    Enumerated from the tree: every shipped manifest parses, builds and binds, every
    render's producer is one the engine can call, every replicating handler asks only for
    what the peer hands over, and no shipped name earns the warning.
    """
    assert len(SHIPPED) >= 8, SHIPPED
    producers, replicating, rules = [], [], 0
    for path in SHIPPED:
        name = os.path.basename(path)
        assert check(path) == [], path
        assert re.match(PLUGIN_NAME_PATTERN, name), name
        loader._purge(name)
        manifest, backend = loader._from_directory(name, path)
        rules += sum(len(fields) for fields in manifest.schemas.values())
        for endpoint in manifest.endpoints:
            if endpoint.render is not None:
                loader._render_producer(endpoint, backend)
                producers.append(f"{name}/{endpoint.path}")
            elif endpoint.replicate:
                handler = getattr(backend, endpoint.handler)
                assert binder.replication_problems(handler, endpoint) == [], endpoint.path
                replicating.append(f"{name}/{endpoint.path}")
    # So that none of the loops above passed by having nothing to look at.
    assert len(producers) >= 5, producers
    assert len(replicating) >= 5, replicating
    assert rules >= 100, rules

    # And through the load path itself, which is what a controller runs at start-up.
    said = _listen(loader.LOGGER)
    try:
        for path in SHIPPED:
            name = os.path.basename(path)
            assert loader.load(rig, name).state == "loaded", name
    finally:
        loader.LOGGER.removeHandler(said)
    assert said.warnings("not named") == [], said.warnings("not named")


PROBE = (
    'class Backend:\n    """What the probe manifest names."""\n\n'
    '    marker = "{marker}"\n\n'
    '    def write(self, node: str, body: dict = None) -> dict:\n'
    '        """One answer."""\n'
    '        return {{"by": "write", "node": node, "body": body}}\n\n'
    '    def write_again(self, node: str, body: dict = None) -> dict:\n'
    '        """Another."""\n'
    '        return {{"by": "write_again", "node": node, "body": body}}\n\n'
    '    def listing(self, since: str = None) -> dict:\n'
    '        """A read."""\n'
    '        return {{"items": [], "since": since}}\n\n'
    '    async def talk(self, websocket=None, caller=None) -> dict:\n'
    '        """A read, or a socket."""\n'
    '        if websocket is not None:\n'
    '            await websocket.close()\n'
    '        return {{}}\n\n'
    '    def produce(self) -> dict:\n'
    '        """A document."""\n'
    '        return {{"produced": True}}\n\n'
    '    def changed(self, **payload) -> None:\n'
    '        """One hook handler."""\n\n'
    '    def changed_again(self, **payload) -> None:\n'
    '        """Another."""\n')

# One manifest that uses every field, so each row below changes one value in it.
BASE = {
    "plugin": "probe",
    "backend": "backend",
    "description": "A plugin that declares one of everything.",
    "schemas": {"change": {
        "value": {"type": "array", "items": "string"},
        "label": {"type": "string", "optional": True, "pattern": "^[a-z]+$",
                  "description": "A label", "example": "a"}}},
    "endpoints": [
        {"path": "things/<node>", "methods": ["PATCH"], "minimum": "manager",
         "handler": "write", "schema": "change", "token": TOKEN_KIND_USER,
         "summary": "Change a thing", "public": False, "forward": False,
         "replicate": False, "description": "Changes one thing.",
         "params": {"node": {"description": "Which node", "example": "node001"}},
         "request_example": {"value": ["a"]}, "errors": {404: "No such thing"}},
        {"path": "list", "methods": ["GET"], "minimum": "reader", "handler": "listing",
         "query": {"since": {"description": "Since when", "example": "monday",
                             "type": "string"}},
         "response": {"description": "The things", "example": {"items": []},
                      "media_type": "application/json"}},
        {"path": "talk", "methods": ["GET"], "minimum": "reader", "handler": "talk",
         "websocket": False},
        {"path": "renders/doc", "methods": ["GET"], "minimum": "reader",
         "handler": "produce",
         "render": {"inputs": ["luna.node"], "coalesce": "2s", "serve": "json",
                    "reconcile": True, "reloads": []}},
    ],
    "hooks": {"luna.node.update": "changed"},
}

WRITE, LIST, TALK, DOC = range(4)


def _endpoint(index: int, **changes):
    """
    A change to one endpoint of the base manifest; a value of None takes the key out.
    """
    def change(document: dict) -> None:
        """
        Apply it.
        """
        for key, value in changes.items():
            if value is None:
                document["endpoints"][index].pop(key, None)
            else:
                document["endpoints"][index][key] = value
    return change


def _inside(*keys, to):
    """
    A change to one value nested in the base manifest, named by the keys that lead to it.
    """
    def change(document: dict) -> None:
        """
        Apply it.
        """
        holder = document
        for key in keys[:-1]:
            holder = holder[key]
        holder[keys[-1]] = to
    return change


MOVES, REFUSED, DECLARED = "moves", "refused", "declared"

# Every field of every model a manifest is parsed into, with a changed value and what
# that change does: it moves what the daemon serves, or the load refuses it. DECLARED is
# the third answer and the one to distrust: nothing but the manifest's digest moves, so
# the field is a statement an author makes and no code reads. The rows that say so are
# asserted to be exactly these, so a field cannot join them without being written here.
FIELDS = {
    ("Manifest", "plugin"): (_inside("plugin", to="another"), REFUSED),
    ("Manifest", "backend"): (_inside("backend", to="other"), MOVES),
    ("Manifest", "description"): (_inside("description", to="Something else."), DECLARED),
    ("Manifest", "schemas"): (
        _inside("schemas", to={"change": {"count": {"type": "integer"}}}), MOVES),
    ("Manifest", "endpoints"): (lambda document: document["endpoints"].pop(LIST), MOVES),
    ("Manifest", "hooks"): (_inside("hooks", to={"luna.node.update": "changed_again"}), MOVES),

    ("Endpoint", "path"): (_endpoint(WRITE, path="items/<node>"), MOVES),
    ("Endpoint", "methods"): (_endpoint(WRITE, methods=["POST"]), MOVES),
    ("Endpoint", "minimum"): (_endpoint(WRITE, minimum="rootus"), MOVES),
    ("Endpoint", "handler"): (_endpoint(WRITE, handler="write_again"), MOVES),
    ("Endpoint", "body_schema"): (_endpoint(WRITE, schema=None), MOVES),
    # A node token is held to no role, so the role has to go with the change.
    ("Endpoint", "token"): (_endpoint(WRITE, token=TOKEN_KIND_NODE, minimum=None), MOVES),
    ("Endpoint", "render"): (_endpoint(DOC, render=None), MOVES),
    ("Endpoint", "summary"): (_endpoint(WRITE, summary="Something else"), MOVES),
    ("Endpoint", "public"): (_endpoint(WRITE, public=True, minimum=None), MOVES),
    ("Endpoint", "forward"): (_endpoint(WRITE, forward=True, minimum=None), MOVES),
    ("Endpoint", "replicate"): (_endpoint(WRITE, replicate=True), MOVES),
    ("Endpoint", "websocket"): (_endpoint(TALK, websocket=True), MOVES),
    ("Endpoint", "description"): (_endpoint(WRITE, description="Something else."), MOVES),
    ("Endpoint", "params"): (_endpoint(WRITE, params=None), MOVES),
    ("Endpoint", "query"): (_endpoint(LIST, query=None), MOVES),
    ("Endpoint", "request_example"): (_endpoint(WRITE, request_example={"value": []}), MOVES),
    ("Endpoint", "response"): (_endpoint(LIST, response=None), MOVES),
    ("Endpoint", "errors"): (_endpoint(WRITE, errors={409: "Taken"}), MOVES),

    ("Render", "inputs"): (
        _inside("endpoints", DOC, "render", "inputs", to=["luna.group"]), MOVES),
    ("Render", "coalesce"): (_inside("endpoints", DOC, "render", "coalesce", to="9s"), MOVES),
    ("Render", "serve"): (_inside("endpoints", DOC, "render", "serve", to=""), MOVES),
    ("Render", "reconcile"): (
        _inside("endpoints", DOC, "render", "reconcile", to=False), DECLARED),
    ("Render", "reloads"): (
        _inside("endpoints", DOC, "render", "reloads", to=["slurmctld"]), DECLARED),

    ("Doc", "description"): (
        _inside("endpoints", LIST, "query", "since", "description", to="Other"), MOVES),
    ("Doc", "example"): (
        _inside("endpoints", LIST, "query", "since", "example", to="friday"), MOVES),
    ("Doc", "type"): (
        _inside("endpoints", LIST, "query", "since", "type", to="integer"), MOVES),

    ("Answer", "description"): (
        _inside("endpoints", LIST, "response", "description", to="Other"), MOVES),
    ("Answer", "example"): (
        _inside("endpoints", LIST, "response", "example", to={"items": [1]}), MOVES),
    ("Answer", "media_type"): (
        _inside("endpoints", LIST, "response", "media_type", to="text/plain"), MOVES),

    ("Rule", "type"): (_inside("schemas", "change", "value", "type", to="object"), MOVES),
    ("Rule", "items"): (_inside("schemas", "change", "value", "items", to="integer"), MOVES),
    ("Rule", "optional"): (_inside("schemas", "change", "label", "optional", to=False), MOVES),
    ("Rule", "pattern"): (
        _inside("schemas", "change", "label", "pattern", to="^[0-9]+$"), MOVES),
    ("Rule", "description"): (
        _inside("schemas", "change", "label", "description", to="Other"), MOVES),
    ("Rule", "example"): (_inside("schemas", "change", "label", "example", to="b"), MOVES),
}

MODELS = (manifests.Manifest, manifests.Endpoint, manifests.Render, manifests.Doc,
          manifests.Answer, manifests.Rule)


def _observed(app, client, headers) -> dict:
    """
    What a loaded probe plugin makes the daemon do, read from where each thing is used:
    the published document, the route table, the render engine, the bound hooks, the
    backend in use, and an answer from the write route.
    """
    entry = registry().plugins["probe"]
    prefix = f"{API_PREFIX}/probe"
    document = app.openapi()
    answer = client.patch(f"{prefix}/things/node001", headers=headers,
                          json={"value": ["a"], "label": "a"})
    return {
        "paths": {path: item for path, item in document["paths"].items()
                  if path.startswith(prefix)},
        "models": {name: item for name, item in document["components"]["schemas"].items()
                   if name.startswith("Probe")},
        "routes": sorted((type(route).__name__, route.path, route.name,
                          sorted(getattr(route, "methods", None) or []))
                         for route in entry.routes),
        "renders": {item.key: (item.coalesce, item.wants("luna.node"), item.wants("luna.group"))
                    for item in renders().renders.values() if item.plugin == "probe"},
        "hooks": {event: handler.__name__ for event, handler in entry.hooks.items()},
        "backend": entry.backend.marker,
        "answer": (answer.status_code, answer.json()),
    }


def test_every_manifest_field_moves_something_or_is_refused(rig):
    """
    Derived from the models, so a field added to one of them fails here until it has a
    row, and a row says what the field is for by what moves when its value changes. A
    field that parses, is stored and then read by nothing is how a manifest comes to
    promise what the daemon does not do.
    """
    declared = {(model.__name__, name) for model in MODELS for name in model.model_fields}
    assert set(FIELDS) == declared, (
        f"fields with no row: {sorted(declared - set(FIELDS))}; "
        f"rows for no field: {sorted(set(FIELDS) - declared)}")

    client = TestClient(rig)
    headers = _token(client)

    def loaded(document: dict):
        """
        The digest and what is observed with this manifest loaded, or the refusal.
        """
        _write("probe", document, PROBE.format(marker="backend"),
               PROBE.format(marker="other"))
        try:
            entry = loader.load(rig, "probe")
        except PluginRefused as refused:
            return refused.message, None
        return entry.digest, _observed(rig, client, headers)

    digest, base = loaded(copy.deepcopy(BASE))
    assert base is not None, digest
    assert base["answer"][0] == 200, base["answer"]
    assert base["renders"] and base["hooks"] and base["models"], base

    wrong, outcomes = [], {}
    for key, (change, expected) in FIELDS.items():
        document = copy.deepcopy(BASE)
        change(document)
        assert document != BASE, f"{key}: the row changes nothing"
        said, seen = loaded(document)
        if seen is None:
            outcomes[key] = REFUSED
        elif seen != base:
            outcomes[key] = MOVES
        elif said != digest:
            outcomes[key] = DECLARED
        else:
            outcomes[key] = "nothing at all"
        if outcomes[key] != expected:
            wrong.append(f"{key}: expected {expected}, and it {outcomes[key]}: {said}")
        # Each row starts from the base as loaded, not from the row before it.
        assert loaded(copy.deepcopy(BASE)) == (digest, base), key
    assert wrong == [], wrong

    # Named, so that a field read by nothing is a decision somebody can see.
    assert sorted(key for key, outcome in outcomes.items() if outcome == DECLARED) == [
        ("Manifest", "description"), ("Render", "reconcile"), ("Render", "reloads")]


def _collected(schema, wanted: str, at: str = "") -> dict:
    """
    Every `wanted` keyword in a JSON Schema, by where it sits.
    """
    found = {}
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == wanted and isinstance(value, (list, str)):
                found[at] = value
            else:
                found.update(_collected(value, wanted, f"{at}/{key}" if at else key))
    return found


def _write_route(**changes) -> dict:
    """
    A manifest with one writing route, for asking the parse about a single value.
    """
    return _document({"path": "things/<node>", "methods": ["PATCH"], "minimum": "manager",
                      "handler": "write", **changes})


def _accepted(document: dict) -> bool:
    """
    Whether the parse takes this manifest and its schemas build.
    """
    try:
        manifests.build_models(manifests.parse(document))
    except PluginRefused:
        return False
    return True


def test_the_published_schema_agrees_with_the_models():
    """
    The schema is written by hand beside the models, which is how it comes to publish a
    field, a value or a name the loader does not hold to. Its properties are compared with
    the models' fields, and every enum and pattern in it with what the parse does.
    """
    schema = manifest_schema()
    endpoint = schema["properties"]["endpoints"]["items"]
    rule = schema["properties"]["schemas"]["additionalProperties"]["additionalProperties"]
    published = {
        "Manifest": (manifests.Manifest, schema),
        "Endpoint": (manifests.Endpoint, endpoint),
        "Render": (manifests.Render, endpoint["properties"]["render"]),
        "Doc of a query": (manifests.Doc, endpoint["properties"]["query"]["additionalProperties"]),
        "Doc of a variable": (manifests.Doc,
                              endpoint["properties"]["params"]["additionalProperties"]),
        "Answer": (manifests.Answer, endpoint["properties"]["response"]),
        "Rule": (manifests.Rule, rule),
    }
    for name, (model, section) in published.items():
        fields = sorted(field.alias or key for key, field in model.model_fields.items())
        assert sorted(section["properties"]) == fields, name
        assert section["additionalProperties"] is False, name

    render = {"path": "renders/doc", "methods": ["GET"], "render": {"inputs": ["luna.node"]}}
    one_of = "properties/endpoints/items/properties"
    rules = "properties/schemas/additionalProperties/additionalProperties/properties"
    # Each enum the schema publishes: the values the code holds, and a manifest carrying
    # one value of it, so the parse is asked the same question an editor is.
    enums = {
        f"{one_of}/methods/items": (HTTP_VERBS, lambda value: _write_route(
            methods=[value], **({"minimum": "reader"} if value == "GET" else {}))),
        f"{one_of}/minimum": (MINIMUMS, lambda value: _write_route(minimum=value)),
        f"{one_of}/token": ((TOKEN_KIND_USER, TOKEN_KIND_NODE), lambda value: _write_route(
            token=value, **({"minimum": None} if value == TOKEN_KIND_NODE else {}))),
        f"{one_of}/render/properties/serve": (manifests.SERVES, lambda value: _document(
            {**render, "render": {**render["render"], "serve": value},
             **({"minimum": "reader"} if value else {})})),
        f"{one_of}/query/additionalProperties/properties/type": (
            manifests.QUERY_TYPES, lambda value: _document(
                {**READ, "query": {"since": {"type": value}}})),
        f"{one_of}/params/additionalProperties/properties/type": (
            (manifests.PATH_TYPE,), lambda value: _document(
                {**READ, "params": {"name": {"type": value}}})),
        f"{rules}/type": (manifests.TYPES, lambda value: _document(
            READ, schemas={"change": {"value": {"type": value}}})),
        f"{rules}/items": (manifests.TYPES, lambda value: _document(
            READ, schemas={"change": {"value": {"type": "array", "items": value}}})),
    }
    found = _collected(schema, "enum")
    assert sorted(found) == sorted(enums), sorted(set(found) ^ set(enums))
    for where, (held, carrying) in enums.items():
        assert found[where] == list(held), where
        for value in held:
            document = carrying(value)
            document["endpoints"][0] = {key: item for key, item
                                        in document["endpoints"][0].items()
                                        if item is not None}
            assert _accepted(document), (where, value)
        assert not _accepted(carrying("no-such-value")), where

    # Each pattern: what it takes the code takes, and what the code refuses it refuses.
    # Not the reverse - a duration may be written `2 S` and a plugin named as it likes,
    # and the schema publishes the one spelling of each.
    def duration(text: str) -> bool:
        """
        Whether the loader reads this as a coalesce.
        """
        try:
            seconds(text, 2)
        except ValueError:
            return False
        return True

    patterns = {
        "properties/plugin": (
            lambda text: bool(scaffold.NAME_RULE.match(text)),
            ("slurm", "my-plugin", "a1", "My_Plugin", "1st", "-dash", "")),
        f"{one_of}/render/properties/coalesce": (
            duration, ("2s", "10", "5m", "1h", "2d", "soon", "2 weeks", "-1s", "s")),
        f"{one_of}/errors/propertyNames": (
            lambda text: not _refusal(lambda: manifests.parse(_write_route(
                errors={int(text): "why"}))),
            ("400", "404", "502", "599", "399", "600", "200")),
        "properties/hooks/propertyNames": (
            lambda text: not _refusal(lambda: manifests.parse(_write_route() | {
                "hooks": {text: "changed"}})),
            ("luna.node.update", "a.b.c", "luna.node", "a..c", "a.b.c.d", "plain")),
    }
    found = _collected(schema, "pattern")
    assert sorted(found) == sorted(patterns), sorted(set(found) ^ set(patterns))
    for where, (taken, samples) in patterns.items():
        answers = {text: (bool(re.search(found[where], text)), taken(text))
                   for text in samples}
        assert any(both == (True, True) for both in answers.values()), (where, answers)
        assert any(both == (False, False) for both in answers.values()), (where, answers)
        apart = {text: both for text, both in answers.items() if both[0] and not both[1]}
        assert apart == {}, (where, apart)
    # One constant behind the name: the schema, the scaffold and the loader's warning.
    assert found["properties/plugin"] == PLUGIN_NAME_PATTERN == scaffold.NAME_RULE.pattern
