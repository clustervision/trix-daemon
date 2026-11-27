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
What a plugin declares, and the request models built from it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import hashlib
import json
import re
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, create_model, model_validator

from trixd.common.constants import (HOOK_EVENT_PARTS, HTTP_VERBS, MINIMUMS,
                                   NODE_PATH_PARAMS, TOKEN_KIND_NODE, TOKEN_KIND_USER)
from trixd.common.errors import PluginRefused

# <name> is one path segment; <path:name> is the rest of the path, slashes and all.
PATH_VARIABLE = re.compile(r"<(?:(path):)?([a-zA-Z_][a-zA-Z0-9_]*)>")

# What a declared query parameter may be; the document shows it and FastAPI checks it.
QUERY_TYPES: dict = {"string": str, "integer": int, "boolean": bool}
# What a path variable is, always: the only type a `params` entry may declare.
PATH_TYPE: str = "string"
# Names the binder hands a handler itself, so a query parameter cannot take them.
RESERVED_ARGUMENTS: tuple = ("body", "caller", "request")
# The verbs that carry a body. The binder reads this to decide what to ask FastAPI for,
# and the parse refuses a schema on anything else, so the two cannot drift apart.
BODY_VERBS: tuple = ("POST", "PATCH", "PUT")
# What a render's `serve` may say: nothing, or the one form the reader answers in.
SERVES: tuple = ("", "json")
# A caller chooses the names of the fields it sends, so a log line bounds what it repeats.
UNDECLARED_SHOWN: int = 10
UNDECLARED_NAME_LENGTH: int = 64

TYPES: dict = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "date": date,
    "object": dict,
    "array": list,
}


class Rule(BaseModel):
    """
    One field of a declared schema: the keys a rule may carry, which the parse and the
    published schema both enumerate from here.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = "string"
    items: str = "string"
    optional: bool = False
    pattern: str = ""
    description: str = ""
    example: Any = None


class RequestBody(BaseModel):
    """
    What every request model is built on: it remembers the names of the fields a caller
    sent that the schema does not declare, which validation then drops as it always has.
    """

    _undeclared: tuple = PrivateAttr(default=())

    @model_validator(mode="wrap")
    @classmethod
    def _note_undeclared(cls, data, handler):
        """
        Validate as declared, then keep the names that were left out.
        """
        body = handler(data)
        if isinstance(data, dict):
            body._undeclared = tuple(str(key) for key in data if key not in cls.model_fields)
        return body


def undeclared(body) -> str:
    """
    The fields a body carried that its schema does not declare, or nothing. By name and
    never by value, since a body carries passwords; quoted and bounded, since the caller
    chose the names.
    """
    names = getattr(body, "_undeclared", ())
    shown = ", ".join(repr(name[:UNDECLARED_NAME_LENGTH]) for name in names[:UNDECLARED_SHOWN])
    more = len(names) - UNDECLARED_SHOWN
    return f"{shown} and {more} more" if more > 0 else shown


class Render(BaseModel):
    """
    A derived output: what it reads, how long a burst is gathered, whether it is served, and
    whether producing it changes anything outside this process.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: list = Field(default_factory=list)
    coalesce: str = "2s"
    serve: str = ""
    # Undeclared rather than False by default: a manifest written before this field says
    # nothing about side effects, and `reconciles` below reads it exactly as `serve` was
    # read then, so no plugin already in the field stops loading or changes meaning.
    reconcile: Optional[bool] = None
    reloads: list = Field(default_factory=list)

    @property
    def reconciles(self) -> bool:
        """
        Whether producing this writes files or reloads a service.

        Serving and writing are not opposites - the slurm and genders renders do both, and
        what they serve is a report of the last write rather than the document itself - so
        the derivation is only the fallback for a manifest that does not say.
        """
        return not self.serve if self.reconcile is None else self.reconcile


class Doc(BaseModel):
    """
    A path variable or query parameter, as the published document explains it.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    example: Any = None
    type: str = "string"


class Answer(BaseModel):
    """
    What a successful call returns, as the published document shows it.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    example: Any = None
    media_type: str = "application/json"


class Endpoint(BaseModel):
    """
    One path a plugin asks the daemon to serve.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    methods: list = Field(default_factory=lambda: ["GET"])
    # The lowest role on the ladder this endpoint admits. A path that is read at
    # one level and changed at another is two endpoints.
    minimum: str = ""
    handler: str = ""
    body_schema: str = Field(default="", alias="schema")
    token: str = TOKEN_KIND_USER
    render: Optional[Render] = None
    summary: str = ""
    # No token at all. Only for a consumer that cannot present one, which today
    # means Prometheus service discovery. Every use has to be argued for.
    public: bool = False
    # The daemon checks nothing: the request goes on as sent and the service behind it
    # decides, as Luna does behind /api/v1/luna. Only the luna plugin needs it.
    forward: bool = False
    # This call has to happen on the other controller too, because what it writes is not
    # in a replicated table and no render brings it back. Off by default, including for a
    # plugin's own bulk route: a call that need not cross should not.
    replicate: bool = False
    # A WebSocket rather than a request. A browser cannot put a token on the upgrade, so
    # the first message carries it and is judged as a route's token is; the coroutine
    # handler is then handed the accepted socket and the caller.
    websocket: bool = False
    # What /api/v1/docs shows beyond the summary: what the route does, what to send, what
    # comes back, and the refusals its own work can answer.
    description: str = ""
    params: dict[str, Doc] = Field(default_factory=dict)
    query: dict[str, Doc] = Field(default_factory=dict)
    request_example: Any = None
    response: Optional[Answer] = None
    errors: dict[int, str] = Field(default_factory=dict)

    @property
    def variables(self) -> list:
        """
        Every path variable this endpoint declares.
        """
        return [name for _, name in PATH_VARIABLE.findall(self.path)]

    @property
    def route_path(self) -> str:
        """
        The manifest writes <name>; Starlette wants {name}.
        """
        return PATH_VARIABLE.sub(lambda found: "{" + found.group(2) + (
            ":path}" if found.group(1) else "}"), self.path).strip("/")

    @property
    def shape(self) -> str:
        """
        The path as Starlette matches it, which a variable's name is no part of.
        """
        return PATH_VARIABLE.sub(
            lambda found: "{:path}" if found.group(1) else "{}", self.path).strip("/")


class Manifest(BaseModel):
    """
    Everything a plugin declares about itself.
    """

    model_config = ConfigDict(extra="forbid")

    plugin: str
    backend: str = "backend"
    description: str = ""
    schemas: dict = Field(default_factory=dict)
    endpoints: list = Field(default_factory=list)
    # Which changes elsewhere this plugin acts on, and the backend method for each.
    hooks: dict = Field(default_factory=dict)

    @property
    def digest(self) -> str:
        """
        A stable hash of the declaration, so a reload can say what changed.
        """
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def parse(document: dict) -> Manifest:
    """
    Turn a manifest document into a Manifest, refusing anything unusable.
    """
    try:
        manifest = Manifest(**document)
        manifest.endpoints = [item if isinstance(item, Endpoint) else Endpoint(**item)
                              for item in manifest.endpoints]
    except Exception as error:
        raise PluginRefused(f"manifest does not parse: {error}") from None

    if not manifest.plugin:
        raise PluginRefused("manifest names no plugin")
    _check_schemas(manifest)
    for endpoint in manifest.endpoints:
        # In the order an author meets them: each check raises on the first thing it
        # finds, so this order is which refusal a manifest breaking two rules answers.
        _check_path(endpoint)
        _check_verbs(endpoint)
        _check_token_kind(endpoint)
        _check_handler(endpoint)
        _check_websocket(endpoint)
        _check_minimum(endpoint)
        _check_replicate(endpoint)
        _check_render(endpoint)
        _check_unguarded(endpoint)
        _check_node_token(endpoint)
        _check_schema(manifest, endpoint)
        _check_docs(endpoint)
    _check_hooks(manifest)
    return manifest


def duplicates(manifest: Manifest) -> list:
    """
    Every path and verb this manifest declares more than once.

    Starlette matches the first of two routes with the same path and verb, so the second
    handler is dead code its author believes is serving. A variable's name is no part of
    that match, so rules/<alpha> and rules/<beta> are one route and not two. Collected
    rather than raised: the command-line check lists every problem it can find at once so
    an author fixes them in one pass, and a refusal from the parse ends that collection at
    the first one. The load path refuses on whatever comes back here.
    """
    seen, twice = {}, []
    for endpoint in manifest.endpoints:
        where = endpoint.path or "/"
        for verb in endpoint.methods:
            key = (endpoint.shape, verb.upper())
            first = seen.get(key)
            if first == where:
                twice.append(f"{verb.upper()} {where} is declared twice")
            elif first is not None:
                twice.append(f"{verb.upper()} {where} and {first} differ only in the name of "
                             f"a path variable, so only {first} is ever matched")
            seen.setdefault(key, where)
    return twice


def _check_schemas(manifest: Manifest) -> None:
    """
    Refuse a schema that declares nothing, and a rule the request model would not honour.
    """
    for name, fields in manifest.schemas.items():
        if not isinstance(fields, dict) or not fields:
            # Its model would take any body at all and hand the handler an empty one.
            raise PluginRefused(f"schema {name} declares no fields, so every body sent to "
                                f"it would be accepted and reach the handler empty")
        for field, rule in fields.items():
            if rule is None:
                continue
            where = f"schema {name} field {field}"
            if not isinstance(rule, dict):
                raise PluginRefused(f"{where} is not a rule: write it as "
                                    f"{{{', '.join(Rule.model_fields)}}}")
            unknown = [str(key) for key in rule if key not in Rule.model_fields]
            if unknown:
                # Dropped in silence otherwise: `required: false` reads as optional and
                # the field stays required.
                raise PluginRefused(f"{where} declares {', '.join(unknown)}, which is not "
                                    f"one of {', '.join(Rule.model_fields)}")
            for key in ("type", "items"):
                if key in rule and str(rule[key]) not in TYPES:
                    # Taken as a string otherwise, so the body is checked as something
                    # the manifest does not say.
                    raise PluginRefused(f"{where} has {key} {rule[key]}; "
                                        f"one of {', '.join(TYPES)}")


def _check_path(endpoint: Endpoint) -> None:
    """
    Refuse a path variable spelled the router's way, which the binder does not see.
    """
    if "{" in endpoint.path or "}" in endpoint.path:
        # The router would match it as a variable while the handler is never handed it.
        raise PluginRefused(f"{endpoint.path} carries a brace: a path variable is written "
                            f"<name>, and <path:name> for the rest of the path")


def _check_verbs(endpoint: Endpoint) -> None:
    """
    Refuse a verb no route can be built for.
    """
    bad = [verb for verb in endpoint.methods if verb.upper() not in HTTP_VERBS]
    if bad:
        raise PluginRefused(f'{endpoint.path or "/"} declares unsupported verbs: {", ".join(bad)}')


def _check_token_kind(endpoint: Endpoint) -> None:
    """
    Refuse a token kind the gate does not know.
    """
    if endpoint.token not in (TOKEN_KIND_USER, TOKEN_KIND_NODE):
        raise PluginRefused(f'{endpoint.path or "/"} declares an unknown token kind')


def _check_handler(endpoint: Endpoint) -> None:
    """
    Refuse an endpoint with nothing behind it; a render produces its own answer.
    """
    if endpoint.render is None and not endpoint.handler:
        raise PluginRefused(f'{endpoint.path or "/"} names no handler')


def _check_websocket(endpoint: Endpoint) -> None:
    """
    Refuse a socket declared as something a socket cannot be.
    """
    if not endpoint.websocket:
        return
    for refusal, blocked in (
            ("is opened by a GET only",
             [verb.upper() for verb in endpoint.methods] != ["GET"]),
            ("cannot also be a render", endpoint.render is not None),
            ("cannot also be forwarded", endpoint.forward),
            ("cannot also be replicated", endpoint.replicate),
            ("cannot also take a body schema", bool(endpoint.body_schema)),
            # Never published: a socket is not in the API document.
            ("documents no request example", endpoint.request_example is not None),
            ("documents no response", endpoint.response is not None),
            ("documents no errors", bool(endpoint.errors)),
            ("is handed the socket as websocket, so cannot take that name",
             "websocket" in (*endpoint.variables, *endpoint.query))):
        if blocked:
            raise PluginRefused(
                f"{endpoint.path or '/'} is a websocket, which {refusal}")


def _check_minimum(endpoint: Endpoint) -> None:
    """
    Refuse a guarded user-token route that names no role for the gate to read.
    """
    if endpoint.render is not None and not endpoint.render.serve and not endpoint.minimum:
        # A render that serves nothing answers no route, so no gate reads a role for it.
        return
    if (not endpoint.public and not endpoint.forward and endpoint.token == TOKEN_KIND_USER
            and endpoint.minimum not in MINIMUMS):
        raise PluginRefused(
            f'{endpoint.path or "/"} must declare a minimum role from '
            f'{", ".join(MINIMUMS)}, not {endpoint.minimum or "nothing"}')


def _check_replicate(endpoint: Endpoint) -> None:
    """
    Refuse replication where there is nothing for the peer to be told.
    """
    if not endpoint.replicate:
        return
    # A read has nothing to carry; a render is already driven at both ends by the
    # same source; a forwarded call is replicated by the service behind it; and a
    # public one would let anyone make this controller call its peer.
    reads = [verb.upper() for verb in endpoint.methods
             if verb.upper() in ("GET", "HEAD")]
    if len(reads) == len(endpoint.methods):
        raise PluginRefused(f"{endpoint.path or '/'} reads, so there is nothing "
                            f"to replicate")
    for why, blocked in (("a render", endpoint.render is not None),
                         ("forwarded", endpoint.forward),
                         ("public", endpoint.public)):
        if blocked:
            raise PluginRefused(f"{endpoint.path or '/'} is {why} and cannot "
                                f"also replicate")
    if reads:
        # Replicate is applied per verb, so a read sharing the endpoint replicates
        # as well: it writes an outbox row and asks the peer on every call, and
        # answers with a block no read is documented to carry.
        raise PluginRefused(f"{endpoint.path or '/'} replicates, so it cannot also "
                            f"serve {', '.join(reads)}: a read that replicated "
                            f"would write an outbox row for every call")
    if endpoint.query:
        raise PluginRefused(f"{endpoint.path or '/'} replicates, so it cannot also "
                            f"declare the query parameter {', '.join(endpoint.query)}: "
                            f"it would be written down as naming what the call is for, "
                            f"and two calls for one node would not be kept in order")


def _check_render(endpoint: Endpoint) -> None:
    """
    The rules a render alone is held to.
    """
    if endpoint.render is None:
        return
    if endpoint.render.serve not in SERVES:
        # Anything said here is served as JSON, whatever it names.
        raise PluginRefused(f"{endpoint.path or '/'} is a render that serves "
                            f"{endpoint.render.serve}; json, or nothing at all")
    _check_render_inputs(endpoint)
    _check_render_arguments(endpoint)
    _check_render_body(endpoint)
    _check_render_job(endpoint)


def _check_render_inputs(endpoint: Endpoint) -> None:
    """
    Refuse a render nothing would ever wake.
    """
    # Nothing wakes a render with no inputs, and nothing wakes one whose input has
    # an empty segment: the engine matches an input against <source>.<object>, so
    # `luna.` and `` match no change there is. Either way it produces once at
    # start-up, serves that for ever and reports a healthy render while the files
    # it writes drift from Luna. Refused rather than logged, because an author's
    # only other signal is stale data at a customer.
    if not endpoint.render.inputs:
        raise PluginRefused(f"{endpoint.path or '/'} is a render and declares no "
                            f"inputs, so nothing would ever wake it")
    unusable = [str(item) for item in endpoint.render.inputs
                if not all(part.strip() for part in str(item).split("."))]
    if unusable:
        raise PluginRefused(
            f"{endpoint.path or '/'} declares input {', '.join(unusable)}, which "
            f"names no change: an input is <source>.<object>, such as luna.node, "
            f"or one of this plugin's own declarations")


def _check_render_arguments(endpoint: Endpoint) -> None:
    """
    Refuse a render that takes an argument its reader is never given.
    """
    if endpoint.variables or endpoint.query:
        # A render is served by a reader that takes no arguments, so a variable or a
        # query parameter is accepted and then ignored: one document answers for every
        # value of it, the producer is never told which was asked for, and the author's
        # only signal is wrong data at a customer.
        unusable = "a path variable" if endpoint.variables else "a query parameter"
        raise PluginRefused(f"{endpoint.path or '/'} is a render, so it cannot take "
                            f"{unusable}: the reader is given none")


def _check_render_body(endpoint: Endpoint) -> None:
    """
    Refuse a body or a write on a render, which the reader behind it is handed neither of.
    """
    # The reader behind a render is handed neither a body nor a verb of its own. A
    # declared schema is built, documented and dropped on the way in, and any verb but a
    # read reaches that same reader, which answers what was last rendered. Both are
    # accepted today and ignored in silence.
    if endpoint.body_schema:
        raise PluginRefused(
            f"{endpoint.path or '/'} is a render, so it cannot also declare schema "
            f"{endpoint.body_schema}: a body is documented and checked, and then "
            f"the reader is not handed it")
    writes = [verb.upper() for verb in endpoint.methods
              if verb.upper() not in ("GET", "HEAD")]
    if writes:
        raise PluginRefused(
            f"{endpoint.path or '/'} is a render, so it cannot also serve "
            f"{', '.join(writes)}: the reader answers what was last rendered, so "
            f"the document would show a write that changes nothing")


def _check_render_job(endpoint: Endpoint) -> None:
    """
    Refuse a render that neither writes anything nor serves what it produces.
    """
    # A render is one of two jobs and the manifest has to say which: a document
    # built in memory and served, or a task that makes files on disk agree with the
    # state and reloads whatever reads them. The two shapes below say neither.
    if not endpoint.render.reconciles and not endpoint.render.serve:
        raise PluginRefused(
            f"{endpoint.path or '/'} is a render declaring reconcile: false and no "
            f"serve, so it writes nothing and nothing reads what it produces: it "
            f"would run on every wake for no effect an author could observe")
    if endpoint.render.reloads and not endpoint.render.reconciles:
        raise PluginRefused(
            f"{endpoint.path or '/'} is a pure render, so it cannot declare that it "
            f"reloads {', '.join(map(str, endpoint.render.reloads))}: only a "
            f"reconcile reaches a service")


def _check_unguarded(endpoint: Endpoint) -> None:
    """
    Refuse a route whose declaration reads as a guard that is not applied to it.
    """
    unguarded = "public" if endpoint.public else "forward" if endpoint.forward else ""
    if unguarded and endpoint.token == TOKEN_KIND_NODE:
        # Which half the author meant - the unguarded route or the node binding -
        # cannot be derived from the declaration, so neither is assumed.
        raise PluginRefused(
            f"{endpoint.path or '/'} is {unguarded} and takes a node token: such a "
            f"route is served with no guard, so no token is asked for or bound")
    ignores_role = unguarded or ("on a node token"
                                 if endpoint.token == TOKEN_KIND_NODE else "")
    if ignores_role and endpoint.minimum:
        # The gate reads `minimum` on a guarded user-token route and nowhere else: a
        # node token carries no principal, and an unguarded route is not asked. The
        # manifest then reads as a restriction that is not applied anywhere.
        raise PluginRefused(
            f"{endpoint.path or '/'} is {ignores_role} and declares minimum "
            f"{endpoint.minimum}: no role is checked on such a route, so the "
            f"declaration admits everyone it reads as keeping out")


def _check_node_token(endpoint: Endpoint) -> None:
    """
    Refuse a node-token route whose path gives the gate no node to bind the token to.
    """
    if endpoint.token == TOKEN_KIND_NODE and not any(
            key in endpoint.variables for key in NODE_PATH_PARAMS):
        # The gate binds a node token to the node its path names, and looks for it under
        # one of these names only. With none of them there the binding is skipped in
        # silence and one node's token is accepted for another node's data.
        raise PluginRefused(
            f"{endpoint.path or '/'} takes a node token, so its path has to carry one "
            f"of {', '.join(NODE_PATH_PARAMS)} for the gate to bind it to")


def _check_schema(manifest: Manifest, endpoint: Endpoint) -> None:
    """
    Refuse a body schema that is not declared, or declared on a verb that takes no body.
    """
    if not endpoint.body_schema:
        return
    if endpoint.body_schema not in manifest.schemas:
        raise PluginRefused(
            f"{endpoint.path} wants schema {endpoint.body_schema}, which is not declared")
    # GET is deliberate: a GET and a write on one handler tell a read from a change
    # by the body being None. Any other bodiless verb is accepted here and dropped
    # at bind, so the document agrees with the route and not with the manifest.
    bodiless = [verb.upper() for verb in endpoint.methods
                if verb.upper() not in BODY_VERBS and verb.upper() != "GET"]
    if bodiless:
        raise PluginRefused(
            f"{endpoint.path or '/'} wants schema {endpoint.body_schema} on "
            f"{', '.join(bodiless)}, which takes no body")


def _check_hooks(manifest: Manifest) -> None:
    """
    Refuse a hook that names no event or no handler.
    """
    for event, handler in manifest.hooks.items():
        parts = str(event).split(".")
        if len(parts) != HOOK_EVENT_PARTS or not all(parts):
            raise PluginRefused(f"{event} is not a hook event: name one as "
                                f"<source>.<object>.<event>, such as luna.node.update")
        if not handler or not isinstance(handler, str):
            raise PluginRefused(f"{event} names no handler")


def _check_docs(endpoint: Endpoint) -> None:
    """
    Refuse documentation that the document could not show truthfully.
    """
    where = endpoint.path or "/"
    unknown = [name for name in endpoint.params if name not in endpoint.variables]
    if unknown:
        raise PluginRefused(f"{where} documents {', '.join(unknown)}, which its path does not have")
    typed = [f"{name} as {item.type}" for name, item in endpoint.params.items()
             if item.type != PATH_TYPE]
    if typed:
        # A path variable reaches the handler as text whatever is declared for it.
        raise PluginRefused(f"{where} documents path variable {', '.join(typed)}; a path "
                            f"variable is always a {PATH_TYPE}")
    # A query parameter by one of these names is already refused below. A path variable by
    # one of them was not, and for a public endpoint nothing else catches it: the handler is
    # handed the path's value where it expects the caller, and answers 500 on its first call.
    taken = [name for name in endpoint.variables if name in RESERVED_ARGUMENTS]
    if taken:
        raise PluginRefused(f"{where} names path variable {', '.join(taken)}, which is how a "
                            f"handler is given its body, its caller or the request")
    for name, item in endpoint.query.items():
        if (not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) or name in RESERVED_ARGUMENTS
                or name in endpoint.variables):
            raise PluginRefused(f"{where} declares query parameter {name}, which it cannot take")
        if item.type not in QUERY_TYPES:
            raise PluginRefused(f"{where} query parameter {name} is {item.type}; "
                                f"one of {', '.join(QUERY_TYPES)}")
    bad = [str(status) for status in endpoint.errors if not 400 <= status < 600]
    if bad:
        raise PluginRefused(f"{where} documents {', '.join(bad)} as errors; 400 to 599 only")


def build_models(manifest: Manifest) -> dict:
    """
    Make one request model per declared schema.
    """
    models = {}
    for name, fields in manifest.schemas.items():
        spec = {}
        for field, rule in (fields or {}).items():
            rule = rule or {}
            kind = TYPES.get(str(rule.get("type", "string")), str)
            if kind is list:
                item = TYPES.get(str(rule.get("items", "string")), str)
                kind = list[item]
            constraints = {}
            if rule.get("pattern"):
                constraints["pattern"] = rule["pattern"]
            if rule.get("description"):
                constraints["description"] = str(rule["description"])
            if "example" in rule:
                constraints["examples"] = [rule["example"]]
            if rule.get("optional"):
                spec[field] = (Optional[kind], Field(default=None, **constraints))
            else:
                spec[field] = (kind, Field(**constraints))
        try:
            models[name] = create_model(f"{manifest.plugin.title()}{name.title()}",
                                        __base__=RequestBody, **spec)
        except Exception as error:
            raise PluginRefused(f"schema {name} does not build: {error}") from None
    return models


def read_yaml(path: str) -> dict:
    """
    Read a manifest.yaml from disk.
    """
    import yaml
    with open(path, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise PluginRefused(f"{path} is not a manifest document")
    return document
