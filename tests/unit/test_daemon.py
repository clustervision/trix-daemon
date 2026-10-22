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
The properties the daemon has to keep, rather than the behaviour of one endpoint.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import contextlib
import inspect
import logging as logmodule
import os
import re
from datetime import datetime, timedelta, timezone

import jwt
import pytest

import trixd

from tests.conftest import SHARED_SECRET
from trixd.common import settings as configuration
from trixd.common.constants import API_PREFIX, LUNA_PLUGIN, MINIMUMS, RESERVED_PLUGINS
from trixd.common.errors import PluginRefused, Unauthenticated
from trixd.core.plugins import loader
from trixd.core.plugins.manifest import parse
from trixd.core.security import audit, tokens


def _serves(app, path: str) -> bool:
    """
    Whether the route table carries this exact path.
    """
    return any(getattr(route, "path", None) == path for route in app.router.routes)


def test_health_needs_no_token(client):
    """
    A health check that needs a token is not one; so it carries states, never error text.
    """
    answer = client.get(f"{API_PREFIX}/system/health")
    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "ok"
    assert "luna_hashes" in body["triggers"] and "replication" in body["triggers"]
    assert all(set(item) == {"render", "rendered_at", "ok"} for item in body["renders"])


def test_token_is_a_jwt_signed_the_way_luna_signs(token):
    """
    HS256 by default, and no key identifier, because Luna emits neither.
    """
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "HS256"
    assert "kid" not in header
    claims = tokens.verify(token)
    assert claims.subject == "tester"
    assert claims.kind == "user"


def test_luna_verifies_a_token_this_daemon_minted(token):
    """
    The shared secret is the whole point: Luna decodes this without our help.
    """
    claims = jwt.decode(token, SHARED_SECRET, algorithms=["HS256"])
    # Luna knows a caller by id alone. The ini account is its id 0, the
    # administrator its own ini names, so the two agree on who that is.
    assert claims.get("scope") is None
    assert claims["sub"] == "tester"
    assert claims["id"] == 0


def test_a_personal_token_is_nobody_at_luna(rig):
    """
    Luna reads a missing id as its administrator, so every other login names one it never issues.
    """
    minted = tokens.mint("alice", source="pam")
    claims = jwt.decode(minted, SHARED_SECRET, algorithms=["HS256"])
    assert claims["id"] == -1
    assert tokens.verify(minted).subject == "alice"


def test_a_service_kind_token_is_nobody_at_luna(rig):
    """
    A token naming this daemon rather than a person carries no Luna identity, so Luna
    refuses it rather than obeying it.
    """
    minted = tokens.mint("trixd", kind="service")
    claims = jwt.decode(minted, SHARED_SECRET, algorithms=["HS256"])
    assert claims["id"] == -1


def test_a_node_token_carries_lunas_provision_scope(rig):
    """
    Luna confines a provision token to the node it names, so we say which node.
    """
    minted = tokens.mint("node001", kind="node", node="node001")
    claims = jwt.decode(minted, SHARED_SECRET, algorithms=["HS256"])
    assert claims["scope"] == "provision"
    assert claims["node"] == "node001"
    # Luna never asks a provision token who it is, so it names no one.
    assert "id" not in claims


def test_a_token_luna_minted_is_accepted_here(rig):
    """
    Luna names an id and no subject. Id 0 is the administrator its ini names.
    """
    expiry = datetime.now(timezone.utc) + timedelta(seconds=300)
    admin = jwt.encode({"id": 0, "exp": expiry}, SHARED_SECRET, "HS256")
    claims = tokens.verify(admin)
    assert claims.kind == "user"
    assert claims.subject == "tester"
    assert claims.source == "luna"

    node = jwt.encode({"node": "node001", "scope": "provision", "exp": expiry},
                      SHARED_SECRET, "HS256")
    claims = tokens.verify(node)
    assert claims.kind == "node"
    assert claims.node == "node001"


def test_a_token_with_no_expiry_is_refused(rig):
    """
    Verification honoured an expiry when the token carried one and said nothing when it did
    not, so a token minted without one never expired - correctly signed, valid until the
    secret is rotated, and only cut by disabling the user. The sweep already refuses to hold
    such a token; this is the same demand where tokens arrive.

    Signed with the secret shared with Luna, because that is the realistic source of one:
    anything that may mint for either daemon could omit it.
    """
    forever = jwt.encode({"id": 0}, SHARED_SECRET, "HS256")
    with pytest.raises(Unauthenticated):
        tokens.verify(forever)

    expiry = datetime.now(timezone.utc) + timedelta(seconds=300)
    bounded = jwt.encode({"id": 0, "exp": expiry}, SHARED_SECRET, "HS256")
    assert tokens.verify(bounded).subject == "tester", "a token with an expiry was refused"


def test_a_token_signed_with_another_secret_is_refused(rig):
    """
    Sharing a secret with Luna is not sharing it with everybody.
    """
    expiry = datetime.now(timezone.utc) + timedelta(seconds=300)
    forged = jwt.encode({"id": 0, "exp": expiry}, "a0000000000000000000000000000000", "HS256")
    with pytest.raises(Unauthenticated):
        tokens.verify(forged)


def test_the_keypair_algorithm_still_works(keypair_rig):
    """
    EdDSA stays selectable, with the key identifier back in the header.
    """
    minted = tokens.mint("tester")
    header = jwt.get_unverified_header(minted)
    assert header["alg"] == "EdDSA"
    assert header["kid"] == "trixd-test"
    assert tokens.verify(minted).subject == "tester"


def test_a_token_carries_no_permissions(token):
    """
    The token says who. The store says what.
    """
    claims = tokens.verify(token).raw
    for forbidden in ("capabilities", "permissions", "roles", "level", "grants"):
        assert forbidden not in claims


def test_an_endpoint_refuses_without_a_token(client):
    """
    No token at all is a 401, before any handler runs.
    """
    answer = client.get(f"{API_PREFIX}/directory/users")
    assert answer.status_code == 401


def test_every_user_endpoint_declares_a_minimum_role():
    """
    Read from every shipped manifest, not only the ones a test loads: it fails on the
    first endpoint that forgets.
    """
    import glob
    import os

    import yaml

    manifests = glob.glob(os.path.join(os.path.dirname(trixd.__file__), "plugins", "*",
                                       "manifest.yaml"))
    assert len(manifests) == 6, manifests
    for path in manifests:
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        for endpoint in document["endpoints"]:
            if endpoint.get("public") or endpoint.get("forward") or endpoint.get("token") == "node":
                continue
            assert endpoint.get("minimum") in MINIMUMS, f"{path}: {endpoint['path']}"


def test_every_hook_a_shipped_plugin_wants_is_published_by_one():
    """
    The daemon holds no list of which events exist, so a plugin may subscribe to one
    nobody publishes and simply never run. That is right for a site's own plugin and
    wrong for a shipped one, where it would be a typo nothing reports. Read from every
    manifest and every publisher, so a new plugin on either side is covered.
    """
    import glob
    import os

    import yaml

    from trixd.plugins.luna import hooks as luna_hooks

    published = set(luna_hooks.PUBLISHES)
    wanted = {}
    for path in glob.glob(os.path.join(os.path.dirname(trixd.__file__), "plugins", "*",
                                       "manifest.yaml")):
        with open(path, encoding="utf-8") as handle:
            for event in (yaml.safe_load(handle).get("hooks") or {}):
                wanted[event] = os.path.basename(os.path.dirname(path))
    assert wanted, "no shipped plugin declares a hook, so this proves nothing"
    unknown = {event: plugin for event, plugin in wanted.items() if event not in published}
    assert unknown == {}, f"subscribed to an event nobody publishes: {unknown}"


def test_the_published_document_matches_the_route_table(rig):
    """
    Enumerate one, assert the other.
    """
    from trixd.api.v1 import auth, identity, system

    document = rig.openapi()
    published = set(document["paths"])
    # The daemon's own routers sit in the table as one entry each, so they are listed
    # from the routers themselves; plugin routes are swapped in one by one.
    routes = list(rig.router.routes)
    for module in (auth, identity, system):
        routes += module.router.routes
    # The document shows {path} where the route says {path:path}.
    registered = {re.sub(r"\{(\w+):\w+\}", r"{\1}", route.path) for route in routes
                  if getattr(route, "include_in_schema", False)
                  and getattr(route, "path", "").startswith(API_PREFIX)}
    assert registered - published == set()
    assert published - registered == set()


def test_every_reserved_name_is_refused(rig):
    """
    No plugin may take a prefix the daemon serves itself.
    """
    assert {"auth", "identity", "system", "docs"} <= set(RESERVED_PLUGINS)
    for name in RESERVED_PLUGINS:
        with pytest.raises(PluginRefused, match="is reserved"):
            loader.load(rig, name)
    from trixd.core.plugins.registry import current as registry

    listed = {entry["name"] for entry in registry().listing()}
    assert not listed & set(RESERVED_PLUGINS), "a refused reserved name is not a plugin"


def test_a_loaded_plugin_says_where_it_was_loaded_from(rig):
    """
    The plugin list names each plugin's own directory, not a path from the clash check.
    """
    from trixd.core.plugins.registry import current as registry

    for name in ("luna", "directory", "services"):
        origin = registry().get(name).origin
        assert os.path.basename(origin.rstrip("/\\")) == name, (name, origin)


def test_a_plugin_route_on_a_daemon_path_is_refused(rig, tmp_path, monkeypatch):
    """
    Defence in depth: even without the reserved list, a route the daemon serves is taken.
    """
    site = tmp_path / "site-plugins" / "system"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("", encoding="utf-8")
    (site / "backend.py").write_text(
        "class Backend:\n    def health(self):\n        return {}\n", encoding="utf-8")
    (site / "manifest.yaml").write_text(
        "plugin: system\nbackend: backend\nendpoints:\n  - path: health\n"
        "    methods: [GET]\n    minimum: none\n    handler: health\n", encoding="utf-8")
    monkeypatch.setattr(loader, "RESERVED_PLUGINS", ())
    with pytest.raises(PluginRefused, match="already served by the daemon itself"):
        loader.load(rig, "system", str(site))


def test_a_manifest_without_a_minimum_role_is_refused():
    """
    An endpoint that names no minimum would be an open door, or a locked one by accident.
    """
    with pytest.raises(Exception) as raised:
        parse({"plugin": "x", "endpoints": [{"path": "a", "methods": ["GET"],
                                             "handler": "a"}]})
    assert "minimum" in str(raised.value)


def test_a_manifest_with_an_unknown_minimum_role_is_refused():
    """
    A role that is not on the ladder would rank at the bottom and look like none.
    """
    with pytest.raises(Exception) as raised:
        parse({"plugin": "x", "endpoints": [{"path": "a", "methods": ["GET"],
                                             "minimum": "king", "handler": "a"}]})
    assert "king" in str(raised.value)


def test_a_manifest_naming_a_missing_handler_is_refused(rig, tmp_path):
    """
    Half a plugin is worse than none, so the whole plugin is refused.
    """
    broken = tmp_path / "plugins" / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text(
        "plugin: broken\nbackend: backend\nendpoints:\n"
        "  - path: a\n    methods: [GET]\n    minimum: none\n"
        "    handler: does_not_exist\n", encoding="utf-8")
    (broken / "backend.py").write_text('class Backend:\n    """Empty."""\n', encoding="utf-8")

    with pytest.raises(PluginRefused) as raised:
        loader.load(rig, "broken")
    assert "does_not_exist" in str(raised.value)


def test_a_plugin_plugs_and_unplugs_while_the_daemon_runs(rig, client, headers):
    """
    The whole point of the loader: no restart, and the document follows.
    """
    listed = f"{API_PREFIX}/directory/users"
    assert _serves(rig, listed)

    answer = client.post(f"{API_PREFIX}/system/plugins/directory/disable", headers=headers)
    assert answer.status_code == 200
    assert not _serves(rig, listed)
    assert listed not in rig.openapi()["paths"]
    assert client.get(listed, headers=headers).status_code == 404

    answer = client.post(f"{API_PREFIX}/system/plugins/directory/reload", headers=headers)
    assert answer.status_code == 200
    assert _serves(rig, listed)
    assert listed in rig.openapi()["paths"]


def test_a_refused_reload_changes_nothing(rig, client, headers, tmp_path):
    """
    The previously loaded plugin keeps serving, and the call says why.
    """
    listed = f"{API_PREFIX}/services"
    before = len(rig.router.routes)

    manifest = tmp_path / "plugins" / "services" / "manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(
        "handler: list_services", "handler: no_such_method"), encoding="utf-8")

    answer = client.post(f"{API_PREFIX}/system/plugins/services/reload", headers=headers)
    assert answer.status_code == 409
    assert "no_such_method" in answer.json()["message"]
    assert len(rig.router.routes) == before
    assert _serves(rig, listed)


def test_no_path_is_served_twice(rig):
    """
    Except under the reserved Luna subtree, which is a single catch-all.
    """
    seen = {}
    for route in rig.router.routes:
        path = getattr(route, "path", "")
        if not path.startswith(API_PREFIX) or LUNA_PLUGIN in path:
            continue
        for method in getattr(route, "methods", set()):
            key = (method, path)
            assert key not in seen, f"{method} {path} is served twice"
            seen[key] = True


def test_a_node_token_is_refused_on_a_user_endpoint(rig, client):
    """
    A node carries a token for itself and nothing else.
    """
    node_token = tokens.mint("node001", kind="node", node="node001")
    answer = client.get(f"{API_PREFIX}/directory/users",
                        headers={"x-access-tokens": node_token})
    assert answer.status_code == 403


def test_a_wrong_password_is_refused(rig, client):
    """
    The source checks the credential, so a wrong password never mints a token.
    """
    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "tester", "password": "not-the-password"})
    assert answer.status_code == 401


def test_a_right_password_is_accepted(rig, client):
    """
    And the shipped administrator still gets in.
    """
    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "tester", "password": "secret"})
    assert answer.status_code == 201
    assert answer.json()["token"]


def test_the_sources_are_the_four_on_the_board(rig):
    """
    local, pam, ldap and oidc, as the whiteboard drew them; nothing else answers.
    """
    import pathlib

    root = pathlib.Path(trixd.__file__).parent / "core" / "security" / "sources"
    found = {path.stem for path in root.glob("*.py") if path.stem != "__init__"}
    assert found == {"local", "pam", "ldap", "oidc"}, found


def _probe_local(handle, patch) -> dict:
    """
    The local source over a principal whose digest the table holds.
    """
    from trixd.core.security.sources import local as local_source

    _local_user("probe", "right")
    return {"source": local_source.Source(handle), "name": "probe",
            "right": "right", "wrong": "wrong"}


def _probe_pam(handle, patch) -> dict:
    """
    The PAM source on the stand-in passwd database and PAM stack the suite already has.
    """
    from tests.unit.test_integrations import _pam

    source, _ = _pam(patch, {"probe": (1500, "/bin/bash")}, password="right")
    return {"source": source, "name": "probe", "right": "right", "wrong": "wrong"}


def _probe_ldap(handle, patch) -> dict:
    """
    The LDAP source against ldap3's own offline directory, where carol binds with right.
    """
    from tests.unit.test_integrations import _ldap

    return {"source": _ldap(patch), "name": "carol", "right": "right", "wrong": "wrong"}


def _probe_oidc(handle, patch) -> dict:
    """
    OpenID Connect has no provider to drive, and so answers for nobody at all yet.
    """
    from trixd.core.security.sources import oidc as oidc_source

    return {"source": oidc_source.Source(handle), "declines": True}


# One probe per source the chain can import, keyed by the module name it imports. A
# source with no provider or server to talk to yet is marked declines, and is held to
# answering for nobody; every other probe hands over a working credential and a wrong
# one. A source that needs something a unit test cannot stand in for belongs here too,
# named, with what it needs - not left out, which reads as covered.
SOURCE_PROBES = {"local": _probe_local, "pam": _probe_pam,
                 "ldap": _probe_ldap, "oidc": _probe_oidc}


def test_no_source_authenticates_without_checking_the_password(rig, monkeypatch):
    """
    A source that answers True on the name alone is an open door, so every source the
    chain can import is driven here: a wrong password and an empty one are refused, and
    the right one still gets in. The set is read from the package the chain imports
    from, so a source added later is driven here too or this test says it is not.
    """
    import importlib
    import pathlib

    from trixd.core.security import chain
    from trixd.db import engine as database

    package = importlib.import_module(chain.PACKAGE)
    root = pathlib.Path(package.__file__).parent
    found = {path.stem for path in root.glob("*.py") if path.stem != "__init__"}
    assert found == set(SOURCE_PROBES), (
        f"no probe for {sorted(found - set(SOURCE_PROBES))} and a probe for no source "
        f"in {sorted(set(SOURCE_PROBES) - found)}: every source the chain can build "
        f"has to be driven by this test")

    for name in sorted(found):
        # Each probe's stand-ins are undone before the next source is built.
        with monkeypatch.context() as patch:
            handle = database.session()
            try:
                probe = SOURCE_PROBES[name](handle, patch)
                source = probe["source"]
                if probe.get("declines"):
                    for credential in ("anything", ""):
                        verdict, said = source.authenticate("anybody", credential)
                        assert verdict is None, (
                            f"{name} answered {verdict} for a name it has no provider "
                            f"for: {said}. Give it a probe with a credential")
                    continue
                person, right, wrong = probe["name"], probe["right"], probe["wrong"]
                verdict, said = source.authenticate(person, wrong)
                assert verdict is False, f"{name} answered {verdict} to a wrong password"
                verdict, said = source.authenticate(person, "")
                assert verdict is not True, f"{name} accepted an empty password: {said}"
                verdict, identity = source.authenticate(person, right)
                assert verdict is True, f"{name} refused the right password: {identity}"
                assert identity["name"] == person
            finally:
                handle.close()


def _local_user(name: str, password: str) -> None:
    """
    A user whose password the local source holds.
    """
    from trixd.core.security import principals
    from trixd.db import engine as database
    from trixd.db import models

    handle = database.session()
    try:
        handle.add(models.Principal(name=name,
                                    password_digest=principals.hash_password(password)))
        handle.commit()
    finally:
        handle.close()


def test_a_source_that_cannot_work_is_skipped(rig, monkeypatch):
    """
    A missing library must not stop a local principal from logging in.
    """
    from trixd.core.security import chain
    from trixd.db import engine as database

    _local_user("alice", "secret")
    monkeypatch.setattr(configuration.current().auth, "chain", ["ldap", "local"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "alice", "secret")
    finally:
        handle.close()
    assert source == "local"
    assert identity["name"] == "alice"


def test_a_refusal_stops_the_chain(rig, monkeypatch):
    """
    A source that knows the name and refuses it is the end of the walk.
    """
    from trixd.core.security import chain
    from trixd.db import engine as database

    _local_user("alice", "secret")
    monkeypatch.setattr(configuration.current().auth, "chain", ["local", "pam"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "alice", "wrong")
    finally:
        handle.close()
    assert identity is None
    assert source == "local"


def test_an_unknown_source_name_is_skipped(rig, monkeypatch):
    """
    A typo in the chain is logged, not a crash and not an open door.
    """
    from trixd.core.security import chain
    from trixd.db import engine as database

    _local_user("alice", "secret")
    monkeypatch.setattr(configuration.current().auth, "chain", ["nosuchsource"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "alice", "secret")
    finally:
        handle.close()
    assert identity is None
    assert source == ""


def test_a_node_token_is_not_issued_on_the_node_name_alone(rig, client, monkeypatch):
    """
    Luna never issues one anonymously, and the token it mints works on Luna too.
    """
    from trixd.api.v1 import auth as auth_routes

    class Stub:
        """
        A Luna that knows the node, records no measurement and enforces nothing.
        """

        def node(self, name):
            """
            The node as this stubbed Luna holds it.
            """
            return {"name": name, "tpm_sha256": ""}

        def enforcing_security(self):
            """
            Whether this stubbed Luna enforces a measurement.
            """
            return False

    monkeypatch.setattr(auth_routes.nodes, "current", lambda: Stub())
    answer = client.post(f"{API_PREFIX}/auth/node/node001", json={"tpm_sha256": ""})
    assert answer.status_code == 401

    answer = client.post(f"{API_PREFIX}/auth/node/node001",
                         json={"username": "tester", "password": "wrong"})
    assert answer.status_code == 401

    answer = client.post(f"{API_PREFIX}/auth/node/node001",
                         json={"username": "tester", "password": "secret"})
    assert answer.status_code == 201


def test_a_known_user_is_not_enough_to_become_a_node(rig, client, headers, monkeypatch):
    """
    Being resolvable is not authority to be a node. This mints the node's own identity and
    Luna honours it as such, so any account the chain knows - a person with a shell on the
    controller - could have asked for any node and been given it, on both daemons. The
    daemon's own account still may, and so does a role that says it may act for a node.
    """
    from trixd.api.v1 import auth as auth_routes

    class Stub:
        """
        A Luna that knows the node and does not enforce a measurement.
        """

        def node(self, name):
            """
            The node as this stubbed Luna holds it.
            """
            return {"name": name, "tpm_sha256": ""}

        def enforcing_security(self):
            """
            Whether this stubbed Luna enforces a measurement.
            """
            return False

    monkeypatch.setattr(auth_routes.nodes, "current", lambda: Stub())
    for name, role in (("bystander", "none"), ("looker", "reader"), ("doer", "operator")):
        made = client.put(f"{API_PREFIX}/identity/users/{name}", headers=headers,
                          json={"role": role, "password": "a long passphrase"})
        assert made.status_code in (200, 201), made.text

    def asking(name):
        """
        That user asking for node001's token.
        """
        return client.post(f"{API_PREFIX}/auth/node/node001",
                           json={"username": name, "password": "a long passphrase"})

    assert asking("bystander").status_code == 401, "role none became a node"
    assert asking("looker").status_code == 401, "a reader became a node"
    assert asking("doer").status_code == 201, "an operator may act for a node and could not"
    assert client.post(f"{API_PREFIX}/auth/node/node001",
                       json={"username": "tester", "password": "secret"}
                       ).status_code == 201, "the daemon's own account was refused"


def test_a_measurement_that_is_not_ascii_is_refused_rather_than_crashing(rig, client,
                                                                           monkeypatch):
    """
    The measurement is compared in constant time now, and the obvious way to write that
    refuses a non-ASCII str outright - which would turn a wrong measurement from a refusal
    into a 500. It arrives off the wire, so it is compared as bytes.
    """
    from trixd.api.v1 import auth as auth_routes

    class Stub:
        """
        A Luna enforcing security, holding one measurement for the node.
        """

        def node(self, name):
            """
            The node as this stubbed Luna holds it.
            """
            return {"name": name, "tpm_sha256": "abc123"}

        def enforcing_security(self):
            """
            Whether this stubbed Luna enforces a measurement.
            """
            return True

    monkeypatch.setattr(auth_routes.nodes, "current", lambda: Stub())
    answer = client.post(f"{API_PREFIX}/auth/node/node001",
                         json={"tpm_sha256": "abc123\u00e9"})
    assert answer.status_code == 401, answer.text
    assert client.post(f"{API_PREFIX}/auth/node/node001",
                       json={"tpm_sha256": "abc123"}).status_code == 201


def test_a_node_token_needs_a_matching_measurement_when_security_is_on(rig, client, monkeypatch):
    """
    With the cluster flag set, only the recorded measurement opens the door.
    """
    from trixd.api.v1 import auth as auth_routes

    class Stub:
        """
        A Luna enforcing security, holding one measurement for the node.
        """

        def node(self, name):
            """
            The node as this stubbed Luna holds it.
            """
            return {"name": name, "tpm_sha256": "abc123"}

        def enforcing_security(self):
            """
            Whether this stubbed Luna enforces a measurement.
            """
            return True

    monkeypatch.setattr(auth_routes.nodes, "current", lambda: Stub())
    assert client.post(f"{API_PREFIX}/auth/node/node001",
                       json={"tpm_sha256": "wrong"}).status_code == 401
    assert client.post(f"{API_PREFIX}/auth/node/node001",
                       json={"username": "tester", "password": "secret"}).status_code == 401
    assert client.post(f"{API_PREFIX}/auth/node/node001",
                       json={"tpm_sha256": "abc123"}).status_code == 201


def test_the_service_command_comes_from_the_ini(rig, monkeypatch):
    """
    A container has no systemd, so the binary must be a setting, not a constant.
    """
    from trixd.plugins.services import backend as services

    seen = []

    def fake_run(argv, timeout=None):
        """
        Record the argv instead of running anything.
        """
        seen.append(argv)
        return type("R", (), {"ok": True, "out": "active", "err": "", "code": 0})()

    monkeypatch.setattr(services.shell, "run", fake_run)
    monkeypatch.setattr(configuration.current().services, "command", "/opt/pods/unitctl")
    services.run_task("restart", "slurmctld")
    services.Backend().status("slurmctld")

    assert seen, "nothing ran"
    assert all(argv[0] == "/opt/pods/unitctl" for argv in seen), seen
    assert ["/opt/pods/unitctl", "restart", "slurmctld"] in seen


def test_the_audit_trail_records_a_login_and_a_refusal(rig, client):
    """
    The table was created and never written. Both outcomes belong in it.
    """
    from trixd.db import engine as database
    from trixd.db import models

    client.post(f"{API_PREFIX}/auth/token",
                json={"username": "tester", "password": "secret"})
    client.post(f"{API_PREFIX}/auth/token",
                json={"username": "tester", "password": "wrong"})

    audit.flush()
    handle = database.session()
    try:
        rows = handle.query(models.Audit).order_by(models.Audit.id).all()
        outcomes = [(r.principal, r.action, r.outcome) for r in rows]
    finally:
        handle.close()

    assert ("tester", "auth.token", "granted") in outcomes, outcomes
    assert ("tester", "auth.token", "refused") in outcomes, outcomes


def test_a_refused_request_is_audited(rig, client, token):
    """
    A refusal that leaves no trace answers the wrong question later.
    """
    from trixd.db import engine as database
    from trixd.db import models

    node_token = tokens.mint("node001", kind="node", node="node001")
    client.get(f"{API_PREFIX}/directory/users", headers={"x-access-tokens": node_token})

    audit.flush()
    handle = database.session()
    try:
        refusals = handle.query(models.Audit).filter(
            models.Audit.outcome == "refused").all()
        details = [r.detail for r in refusals]
    finally:
        handle.close()
    assert any("node token" in d for d in details), details


def test_an_identity_write_leaves_its_audit_row(rig, client, headers):
    """
    A second session here blocks on the request's own write lock and the row is lost.
    """
    from trixd.db import engine as database
    from trixd.db import models

    # put_user flushes before it audits, so the request holds the write lock:
    # a second session here waits out the busy timeout and loses the row.
    client.put(f"{API_PREFIX}/identity/users/audited", json={}, headers=headers)
    audit.flush()
    handle = database.session()
    try:
        rows = handle.query(models.Audit).filter(
            models.Audit.object == "user/audited").all()
        outcomes = [(r.action, r.outcome, r.detail) for r in rows]
    finally:
        handle.close()
    assert ("identity.write", "granted", "created") in outcomes, outcomes


def _as(client, username, password):
    """
    A header set for one person, so a test can act as somebody other than rootus.
    """
    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": username, "password": password})
    assert answer.status_code == 201, answer.text
    return {"x-access-tokens": answer.json()["token"]}


def _principal(client, headers, name, role=None):
    """
    One login with a role on the ladder, or with none when no role is given.
    """
    body = {"password": "pw"}
    if role is not None:
        body["role"] = role
    answer = client.put(f"{API_PREFIX}/identity/users/{name}", json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    return _as(client, name, "pw")


def test_no_role_holds_a_token_and_nothing_more(rig, client, headers):
    """
    The bottom of the ladder may ask who it is, and may not even read.
    """
    nina = _principal(client, headers, "nina")
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=nina).json()["role"] == "none"
    refused = client.get(f"{API_PREFIX}/services/catalogue", headers=nina)
    assert refused.status_code == 403, refused.text
    assert "reader" in refused.text and "none" in refused.text
    assert client.get(f"{API_PREFIX}/system/plugins", headers=nina).status_code == 403


def test_a_reader_may_look_and_not_change(rig, client, headers):
    """
    Read-only access, for an auditor or anyone who only needs to see: every read, no change.
    """
    rita = _principal(client, headers, "rita", "reader")
    assert client.get(f"{API_PREFIX}/services/catalogue", headers=rita).status_code == 200
    assert client.get(f"{API_PREFIX}/system/plugins", headers=rita).status_code == 200
    refused = client.patch(f"{API_PREFIX}/services/catalogue", json={}, headers=rita)
    assert refused.status_code == 403, refused.text
    assert "manager" in refused.text and "reader" in refused.text
    acted = client.post(f"{API_PREFIX}/services/nothing/actions/restart", headers=rita)
    assert acted.status_code == 403, acted.text


def test_every_read_route_admits_a_reader_and_refuses_none(rig):
    """
    No shipped plugin leaves a read open to a token with no role.
    """
    import glob

    from trixd.core.plugins.manifest import parse, read_yaml

    for path in glob.glob(os.path.join(os.path.dirname(trixd.__file__), "plugins", "*",
                                       "manifest.yaml")):
        for endpoint in parse(read_yaml(path)).endpoints:
            if endpoint.public or endpoint.token == "node":
                continue
            assert endpoint.minimum != "none", f"{path}: {endpoint.path} admits no role"
            if endpoint.methods == ["GET"]:
                assert endpoint.minimum in ("reader", "rootus"), f"{path}: {endpoint.path}"


def test_an_operator_may_operate_and_not_configure(rig, client, headers):
    """
    Operating a service is the operator's step; changing what it maps to is not.
    """
    ollie = _principal(client, headers, "ollie", "operator")
    acted = client.post(f"{API_PREFIX}/services/nothing/actions/restart", headers=ollie)
    assert acted.status_code not in (401, 403), acted.text
    assert client.patch(f"{API_PREFIX}/services/catalogue", json={},
                        headers=ollie).status_code == 403


def test_a_manager_may_change_what_an_operator_may_not(rig, client, headers):
    """
    Same endpoint, one step up the ladder: that is the axis the design turns on.
    """
    mo = _principal(client, headers, "mo", "manager")
    catalogue = {"services": {"slurmctld": "slurmctld"}}
    assert client.patch(f"{API_PREFIX}/services/catalogue", json=catalogue,
                        headers=mo).status_code == 200


def test_rootus_is_checked_against_nothing(rig, client, headers):
    """
    The account the ini names holds no role and needs none.
    """
    catalogue = {"services": {"slurmctld": "slurmctld"}}
    assert client.patch(f"{API_PREFIX}/services/catalogue", json=catalogue,
                        headers=headers).status_code == 200
    said = client.get(f"{API_PREFIX}/auth/whoami", headers=headers).json()
    assert said["rootus"] is True
    assert said["role"] == "none"


def test_a_role_outside_the_ladder_is_refused(rig, client, headers):
    """
    An unknown role would rank at the bottom and look like a working account.
    """
    answer = client.put(f"{API_PREFIX}/identity/users/king", json={"role": "king"},
                        headers=headers)
    assert answer.status_code == 400
    assert "king" in answer.text


def test_a_name_outside_the_shape_is_refused_by_every_route_that_writes_one(
        rig, client, headers):
    """
    A login name is written to a table that replicates and is addressed on the peer by that
    name, so a route that writes one admits only the shape the chain's sources issue.
    Enumerated from the router, so a route added later has to say which it is.
    """
    from urllib.parse import quote

    from trixd.api.v1 import identity

    writes = sorted({(verb, route.path) for route in identity.router.routes
                     for verb in getattr(route, "methods", ())
                     if "{name}" in route.path
                     and verb in ("POST", "PUT", "PATCH", "DELETE")})
    assert writes, "no identity route writes a user named in its path"
    for verb, path in writes:
        for name in ("a b", 'a"b', "a:b", "x" * 256):
            asked = path.replace("{name}", quote(name, safe=""))
            answer = client.request(verb, asked, json={"role": "reader"}, headers=headers)
            assert answer.status_code == 400, (verb, name, answer.status_code, answer.text)
            assert "is not a login name" in answer.text, (verb, name, answer.text)


def test_whoami_reports_the_role(rig, client, headers):
    """
    A role is what a person can be told; a bit list is not.
    """
    wendy = _principal(client, headers, "wendy", "manager")
    said = client.get(f"{API_PREFIX}/auth/whoami", headers=wendy).json()
    assert said["principal"] == "wendy"
    assert said["role"] == "manager"
    assert said["rootus"] is False


def test_a_refusal_is_in_the_audit_trail(rig, client, headers):
    """
    Who asked, what for, and what they held, or the trail is decoration.
    """
    from trixd.db import engine as database
    from trixd.db import models

    pat = _principal(client, headers, "pat")
    client.patch(f"{API_PREFIX}/services/catalogue", json={}, headers=pat)
    audit.flush()
    handle = database.session()
    try:
        rows = handle.query(models.Audit).filter(models.Audit.principal == "pat",
                                                 models.Audit.outcome == "refused").all()
        details = [(r.action, r.object, r.detail) for r in rows]
    finally:
        handle.close()
    expected = ("PATCH", f"{API_PREFIX}/services/catalogue", "needs manager, holds none")
    assert expected in details, details


def test_a_role_is_set_and_changed_over_the_api(rig, client, headers):
    """
    The one table RBAC has: a role given when the user is made, changed in place, password kept.
    """
    carol = _principal(client, headers, "carol", "operator")
    listed = client.get(f"{API_PREFIX}/identity/users", headers=headers).json()["users"]
    assert {"name": "carol", "role": "operator", "enabled": True, "local": True} in listed
    assert client.put(f"{API_PREFIX}/identity/users/carol", json={"role": "manager"},
                      headers=headers).status_code == 200
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=carol).json()["role"] == "manager"
    assert _as(client, "carol", "pw")


def test_the_oidc_source_lets_the_chain_continue(rig):
    """
    Named before it is built: it must never claim a name, or nobody could log in.
    """
    from trixd.core.security.sources import oidc

    source = oidc.Source()
    assert source.authenticate("tester", "secret")[0] is None
    assert source.resolve("tester")[0] is False


def test_a_rootus_minimum_admits_the_ini_account_only(rig, client, headers):
    """
    The top of the ladder is not a role: a manager is refused where Luna wants rootus.
    """
    mo = _principal(client, headers, "mo", "manager")
    refused = client.get(f"{API_PREFIX}/identity/users", headers=mo)
    assert refused.status_code == 403 and "rootus" in refused.text, refused.text
    assert client.get(f"{API_PREFIX}/system/replication/hashes", headers=mo).status_code == 403
    assert client.get(f"{API_PREFIX}/identity/users", headers=headers).status_code == 200


def test_rootus_cannot_be_given_as_a_role(rig, client, headers):
    """
    It is the ini account by name, never a row, so the API refuses to store it.
    """
    answer = client.put(f"{API_PREFIX}/identity/users/pretender", json={"role": "rootus"},
                        headers=headers)
    assert answer.status_code == 400


def test_a_deleted_principal_is_refused_at_once(rig, client, headers):
    """
    The token stays valid; the row is gone, so the next request cannot resolve it.
    """
    frank = _principal(client, headers, "frank")
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=frank).status_code == 200
    assert client.delete(f"{API_PREFIX}/identity/users/frank",
                         headers=headers).status_code == 200
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=frank).status_code == 401


def test_the_ladder_is_ordered_and_unknown_sits_at_the_bottom(rig):
    """
    none, operator, manager, in that order; a role nobody declared ranks as none.
    """
    from trixd.core.security import access

    assert access.rank("none") < access.rank("operator") < access.rank("manager")
    assert access.rank("manager") < access.rank("rootus")
    assert access.rank("king") == access.rank("none")
    assert access.permits(None, "none") == (True, "none")
    assert access.permits(None, "operator") == (False, "none")
    assert access.permits(None, "rootus") == (False, "none")


def test_a_service_action_is_queued_and_polled(rig, client, headers, monkeypatch):
    """
    The status route: an action answers at once with an id, and the caller polls it.
    """
    from trixd.core import tasks
    from trixd.plugins.services import backend as services

    monkeypatch.setattr(services, "run_task", lambda action, unit: f"{action} {unit} ok")
    tasks.register("services", services.run_task)

    answer = client.post(f"{API_PREFIX}/services/slurmctld/actions/restart",
                         headers=headers)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["state"] == tasks.QUEUED
    identifier = body["request_id"]

    tasks.drain()
    polled = client.get(f"{API_PREFIX}/services/status/{identifier}", headers=headers)
    assert polled.status_code == 200, polled.text
    assert polled.json()["state"] == tasks.DONE
    assert "restart slurmctld ok" in polled.json()["message"]


def test_an_unknown_request_id_is_not_found(rig, client, headers):
    """
    Polling something nobody queued is a 404, not an empty success.
    """
    answer = client.get(f"{API_PREFIX}/services/status/nosuchrequest", headers=headers)
    assert answer.status_code == 404


def test_a_failing_action_is_recorded_as_failed(rig, client, headers, monkeypatch):
    """
    The worker must write down a failure, or a caller polls forever.
    """
    from trixd.common.errors import BackendFailure
    from trixd.core import tasks

    def boom(action, unit):
        """
        A unit that will not start.
        """
        raise BackendFailure(f"{unit} refused to {action}")

    tasks.register("services", boom)
    identifier = tasks.submit("services", "start", "brokend", "tester")
    tasks.drain()

    found = tasks.status(identifier)
    assert found["state"] == tasks.FAILED
    assert "refused to start" in found["message"]


def test_disabling_a_principal_cuts_them_at_once(rig, client, headers):
    """
    The token stays valid; the flag is read per request, so it stops working.
    """
    client.put(f"{API_PREFIX}/identity/users/cut", json={"password": "pw"}, headers=headers)
    theirs = _as(client, "cut", "pw")
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=theirs).status_code == 200

    client.put(f"{API_PREFIX}/identity/users/cut", json={"enabled": False}, headers=headers)
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=theirs).status_code == 401

    client.put(f"{API_PREFIX}/identity/users/cut", json={"enabled": True}, headers=headers)
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=theirs).status_code == 200


def test_a_disabled_principal_cannot_log_in_again(rig, client, headers):
    """
    And the source refuses the password too, not only the resolve.
    """
    assert client.put(f"{API_PREFIX}/identity/users/barred",
                      json={"password": "pw", "enabled": False},
                      headers=headers).status_code == 200
    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "barred", "password": "pw"})
    assert answer.status_code == 401


def test_a_requeued_task_finds_its_handler(rig):
    """
    Starting the worker before the plugins register fails the work it recovers.
    """
    from trixd.core import lifespan

    source = inspect.getsource(lifespan.start)
    assert source.index("loader.load_all") < source.index("tasks.start()"), \
        "the worker must start after the plugins, or a requeued task has no handler"


def test_the_pass_through_recognises_a_node_rename(rig):
    """
    A POST to the node with newnodename or group, answered 2xx, is what a hook must see.
    """
    from trixd.plugins.luna import hooks

    body = b'{"config": {"node": {"node001": {"newnodename": "node002", "comment": "x"}}}}'
    assert hooks.notice("POST", "config/node/node001", body, 204) == {
        "event": "luna.node.update", "payload": {"name": "node001", "newnodename": "node002"}}
    moved = b'{"config": {"node": {"node001": {"group": "gpu"}}}}'
    assert hooks.notice("POST", "config/node/node001", moved, 204) == {
        "event": "luna.node.update", "payload": {"name": "node001", "group": "gpu"}}
    assert hooks.notice("GET", "config/node/node001/_delete", b"", 204) == {
        "event": "luna.node.delete", "payload": {"name": "node001"}}

    plain = b'{"config": {"node": {"node001": {"comment": "x"}}}}'
    assert hooks.notice("POST", "config/node/node001", plain, 204) is None
    assert hooks.notice("POST", "config/node/node001/_clone", body, 201) is None
    assert hooks.notice("POST", "config/node/node001", body, 400) is None
    assert hooks.notice("POST", "config/node/node001", b"not json", 204) is None
    assert hooks.notice("GET", "config/node/node001", b"", 200) is None
    assert hooks.changed_object("POST", "config/node/node001", 204) == "node"
    assert hooks.changed_object("GET", "config/group/g1/_delete", 204) == "group"
    assert hooks.changed_object("GET", "config/node", 200) == ""


def test_a_change_through_the_pass_through_is_queued_for_the_hooks(rig, client, headers,
                                                                    monkeypatch):
    """
    Luna's 204 comes back untouched and the change is on the queue once the answer has left.
    """
    from types import SimpleNamespace

    from trixd.db import engine as database
    from trixd.db import models

    def luna_says(method, url, **kwargs):
        """
        Luna accepting the rename with an empty 204.
        """
        return SimpleNamespace(content=b"", status_code=204, headers={})

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request", luna_says)
    answer = client.post(f"{API_PREFIX}/luna/config/node/node001",
                         json={"config": {"node": {"node001": {"newnodename": "node002"}}}},
                         headers=headers)
    assert answer.status_code == 204

    handle = database.session()
    try:
        row = handle.query(models.Task).filter(models.Task.subsystem == "luna").one()
        assert row.task == "luna.node.update" and '"newnodename": "node002"' in row.param
    finally:
        handle.close()


def test_a_poll_never_produces_a_render(rig):
    """
    A render reads from Luna behind a lock every render shares, and the endpoints that serve
    one need no token. So a poll that produced the render would hold that lock for the whole
    connect timeout with Luna away - and polls arrive every scrape interval and stack, until
    no synchronous endpoint answers, including the one that would unplug the plugin.

    A reader serves what exists and answers 404 otherwise. Prometheus keeps its previous
    targets on a 404, which is a better answer than an empty set.
    """
    from trixd.common.errors import NotFound
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    engine = renders()
    produced = []

    def producer():
        """
        Count the productions, and answer something recognisable.
        """
        produced.append(1)
        return [{"targets": ["counted"]}]

    try:
        item = engine.register("polled", "renders/thing",
                               Render(inputs=["luna.node"], coalesce="1s"), producer)
        # Put it back to holding nothing, whatever registering did, so this is a test of
        # what a poll does and not of what register does.
        item.document = None
        produced.clear()
        read = engine.reader("polled", "renders/thing")

        with pytest.raises(NotFound):
            read()
        with pytest.raises(NotFound):
            read()
        assert produced == [], "a poll produced the render"

        assert engine.run(item) is True
        assert produced == [1]
        assert read() == [{"targets": ["counted"]}]
        assert produced == [1], "serving produced it again"
    finally:
        engine.forget("polled")


def test_a_render_registered_after_the_first_pass_produces_itself(rig, monkeypatch):
    """
    A reload builds new render objects holding no document, and the first pass has long
    gone, so nothing would run them until one of their inputs changed - which for a plugin
    with quiet inputs could be a long time, and now that a poll does not produce one
    either, that window is a 404 rather than a stale answer.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    engine = renders()
    produced = []
    # raising=False so this runs against a build that has no such attribute: otherwise the
    # test errors in setup and proves only that the attribute is new.
    #
    # Both sides of the gate, because the rig has already made its first pass: setting the
    # flag true and asserting a production tells you nothing, since an implementation that
    # produced unconditionally would pass it too.
    monkeypatch.setattr(engine, "reconciled", False, raising=False)
    try:
        engine.register("early", "renders/thing",
                        Render(inputs=["luna.node"], coalesce="1s"),
                        lambda: produced.append("early") or [{"targets": ["early"]}])
        assert produced == [], "it produced before the first pass had been made"

        monkeypatch.setattr(engine, "reconciled", True, raising=False)
        engine.register("reloaded", "renders/thing",
                        Render(inputs=["luna.node"], coalesce="1s"),
                        lambda: produced.append("late") or [{"targets": ["fresh"]}])
        assert produced == ["late"], "a render registered after the first pass was left empty"
        assert engine.reader("reloaded", "renders/thing")() == [{"targets": ["fresh"]}]
    finally:
        engine.forget("reloaded")
        engine.forget("early")


def test_a_reload_never_leaves_a_render_unanswerable(rig, tmp_path):
    """
    Reloading a plugin replaced its renders, and for a moment there was nothing to read:
    forgetting first leaves no render at all, declaring first leaves one that has produced
    nothing. Either way a plugin that is working answers 404 through its own reload, which
    Prometheus sees as service discovery going away.

    The new renders now produce before they are swapped in, so the ones they replace keep
    serving until then. The render itself reports what a reader would have got while it
    ran, which is the only way to observe the window without racing it.
    """
    record = tmp_path / "seen.txt"
    plugin = tmp_path / "plugins" / "swapper"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: swapper\nbackend: backend\nendpoints:\n"
        "  - path: renders/thing\n    methods: [GET]\n    minimum: none\n"
        "    handler: render_thing\n    render:\n      inputs: [luna.node]\n"
        "      serve: json\n", encoding="utf-8")
    (plugin / "backend.py").write_text(
        'RECORD = r"' + str(record) + '"\n\n\n'
        "class Backend:\n"
        '    """Notes what a reader would have been given while it was producing."""\n\n'
        "    def render_thing(self):\n"
        '        """The render, which asks what is readable right now."""\n'
        "        from trixd.core.render.engine import current as renders\n"
        "        try:\n"
        '            got = repr(renders().reader("swapper", "renders/thing")())\n'
        "        except Exception as error:\n"
        "            got = type(error).__name__\n"
        '        with open(RECORD, "a", encoding="utf-8") as handle:\n'
        '            handle.write(got + "\\n")\n'
        '        return {"which": "produced"}\n', encoding="utf-8")

    try:
        loader.load(rig, "swapper")
        loader.load(rig, "swapper")
    finally:
        # Unloaded, or its render stays in the engine for the rest of the session: its
        # inputs name luna.node, so a later test's hook wakes it and the dispatcher says
        # so. The rig does not reset the engine, and a leaked render is not inert.
        loader.unload(rig, "swapper")

    seen = record.read_text(encoding="utf-8").split("\n")[:-1]
    assert len(seen) == 2, f"expected one line per load, got {seen}"
    # Nothing was serving before the first load, so that one has nothing to find.
    assert seen[0] == "NotFound", seen
    assert seen[1] == repr({"which": "produced"}), \
        f"the render being replaced stopped answering during the reload: {seen}"


class _Poller:
    """
    A background trigger that records what was asked of it.
    """

    def __init__(self, name: str, log: dict) -> None:
        """
        Named, so the record says which one.
        """
        self.name = name
        self.log = log

    def baseline(self) -> None:
        """
        Nothing to measure.
        """

    def start(self) -> bool:
        """
        Note that it was started.
        """
        self.log.setdefault("started", []).append(self.name)
        return True

    def shutdown(self) -> None:
        """
        Note that it was stopped.
        """
        self.log.setdefault("stopped", []).append(self.name)

    def state(self) -> dict:
        """
        What health would publish for it.
        """
        return {"answering": True}


def test_a_reload_whose_producer_fails_keeps_what_was_serving(rig):
    """
    The swap took whatever the new render held, and a producer that raises leaves it holding
    nothing - so a reload while Luna is away replaced a document that was answering with a
    404. That is the outcome the ordering was introduced to prevent, caused by the fix.

    The new render keeps what its predecessor was serving and tries again on the next
    change, which is what run() already does for a render that is not being replaced.
    """
    from trixd.common.errors import NotFound
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    engine = renders()
    spec = Render(inputs=["luna.node"], coalesce="1s")

    def served():
        """
        What a poll is given, as a value either way: an unproduced render raises, and this
        test is about what is being served, so the refusal is a result and not an error.
        """
        try:
            return engine.reader("flaky", "renders/thing")()
        except NotFound as refusal:
            return f"nothing: {refusal}"

    try:
        engine.replace("flaky", [("renders/thing", spec, lambda: {"which": "serving"})])
        assert served() == {"which": "serving"}, "nothing was serving to begin with"

        def away():
            """
            A producer that cannot reach Luna.
            """
            raise RuntimeError("Luna is not answering")

        engine.replace("flaky", [("renders/thing", spec, away)])
        assert served() == {"which": "serving"}, \
            "a reload whose producer failed emptied what was answering"

        # And the new producer is the one that runs next, not the old one.
        engine.replace("flaky", [("renders/thing", spec, lambda: {"which": "new"})])
        assert served() == {"which": "new"}
    finally:
        engine.forget("flaky")


def test_unplugging_a_plugin_stops_what_it_registered(rig):
    """
    Unplugging let go of the routes and the renders and nothing else. The task handler
    stayed, so the daemon still accepted a notification naming one of that plugin's events
    and dispatched it into code that had been unplugged; and its trigger kept polling and
    kept waking renders. A disabled plugin was still running.
    """
    from trixd.core import tasks
    from trixd.core.render import triggers

    log = {}
    triggers.register("services_poller", _Poller("mine", log))
    # The owner is recorded straight into the table rather than passed, so this test runs
    # against a build that has no such parameter: there it writes into a throwaway dict
    # and the assertions below still fail for the reason they name, instead of erroring.
    getattr(triggers, "_owners", {})["services_poller"] = "services"
    try:
        assert tasks.handler("services") is not None, "the handler was not there to begin with"
        assert "services_poller" in triggers.states()

        loader.unload(rig, "services")

        assert tasks.handler("services") is None, \
            "a disabled plugin still answers for its own tasks"
        assert log.get("stopped") == ["mine"], \
            f"its trigger was left running: {log}"
        assert "services_poller" not in triggers.states(), "health still lists it"
    finally:
        getattr(triggers, "_triggers", {}).pop("services_poller", None)
        getattr(triggers, "_owners", {}).pop("services_poller", None)


def test_a_replaced_trigger_is_stopped_and_a_late_one_is_started(rig, monkeypatch):
    """
    Registering the same name twice replaced the entry and left the first object running,
    with nothing referencing it - so nothing could stop it, its baseline was the one still
    driving the renders, and health reported the replacement, which had never been started.
    Start happens once at start-up, so a trigger arriving from a later reload never did.
    """
    from trixd.core.render import triggers

    log = {}
    # The rig has already started the daemon's triggers, so this has to be put back to
    # what it is beforehand. raising=False so it also runs against a build with no such
    # flag, where it is simply ignored and the assertions still mean what they say.
    monkeypatch.setattr(triggers, "_running", False, raising=False)
    try:
        triggers.register("swapped", _Poller("first", log))
        assert log.get("started") is None, "it started before the daemon had"

        triggers.register("swapped", _Poller("second", log))
        assert log.get("stopped") == ["first"], \
            f"the trigger that was replaced was left running: {log}"

        monkeypatch.setattr(triggers, "_running", True, raising=False)
        triggers.register("late", _Poller("third", log))
        assert log.get("started") == ["third"], \
            f"a trigger registered after start-up was never started: {log}"

        # The same object offering itself again, which is what reloading a plugin whose
        # trigger is a module-level singleton does - the module holding it is not one the
        # reload purges. It is already running, and starting it again leaves a second
        # thread on the same state, with another on every reload after that.
        same = _Poller("fourth", log)
        triggers.register("same", same)
        triggers.register("same", same)
        assert log["started"].count("fourth") == 1, \
            f"registering the same trigger again started a second one: {log}"
        assert "fourth" not in log.get("stopped", []), \
            f"the same object was stopped as though it had been replaced: {log}"
    finally:
        for name in ("swapped", "late", "same"):
            getattr(triggers, "_triggers", {}).pop(name, None)
            getattr(triggers, "_owners", {}).pop(name, None)


def test_a_plugin_declares_the_changes_it_acts_on(rig, tmp_path, client, headers):
    """
    The manifest names the event and the handler; the dispatcher hands the payload to it.
    """
    from trixd.core import hooks

    plugin = tmp_path / "plugins" / "watcher"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: watcher\nbackend: backend\nhooks:\n  luna.node.update: moved\nendpoints:\n"
        "  - path: seen\n    methods: [GET]\n    minimum: none\n    handler: seen\n",
        encoding="utf-8")
    (plugin / "backend.py").write_text(
        'class Backend:\n    """A plugin that remembers what Luna changed."""\n'
        "    calls = []\n\n"
        '    def moved(self, name, newnodename="", group=""):\n        """Remember."""\n'
        "        self.calls.append((name, newnodename, group))\n        return 'noted'\n\n"
        '    def seen(self):\n        """Show."""\n        return {"calls": self.calls}\n',
        encoding="utf-8")
    loader.load(rig, "watcher")

    said = hooks.run("luna.node.update", '{"name": "node001", "newnodename": "node002"}')
    assert said == "watcher: noted"
    shown = client.get(f"{API_PREFIX}/watcher/seen", headers=headers).json()
    assert shown["calls"] == [["node001", "node002", ""]]
    assert hooks.run("luna.node.delete", '{"name": "node001"}') == "nothing declared luna.node.delete"


def test_a_hook_for_an_unknown_change_is_refused():
    """
    A hook event names who publishes it, what it is about and what happened. The daemon
    checks that shape and nothing more: which events exist belongs to whatever publishes
    them, so a plugin may subscribe to one the daemon has never heard of.
    """
    endpoints = [{"path": "a", "methods": ["GET"], "minimum": "none", "handler": "a"}]
    with pytest.raises(Exception) as raised:
        parse({"plugin": "x", "hooks": {"node.paint": "paint"}, "endpoints": endpoints})
    assert "node.paint" in str(raised.value), "two parts is not an event"

    accepted = parse({"plugin": "x", "hooks": {"switches.port.down": "shut"},
                      "endpoints": endpoints})
    assert accepted.hooks == {"switches.port.down": "shut"}, \
        "the daemon holds no list of which events exist"


def test_the_renders_wake_after_the_hooks_have_run(rig, tmp_path):
    """
    A reconcile woken beside the hook could retire a file the hook was about to move.
    """
    from trixd.core import tasks
    from trixd.plugins.luna import hooks
    from trixd.core.plugins.registry import current as registry

    plugin = tmp_path / "plugins" / "orderly"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: orderly\nbackend: backend\nhooks:\n  luna.node.update: moved\nendpoints:\n"
        "  - path: renders/seen\n    methods: [GET]\n    minimum: none\n"
        "    handler: render_seen\n    render:\n      inputs: [luna.node]\n      serve: json\n",
        encoding="utf-8")
    (plugin / "backend.py").write_text(
        'class Backend:\n    """Remembers the order things happened in."""\n'
        "    calls = []\n\n"
        '    def moved(self, name, newnodename="", group=""):\n        """A hook."""\n'
        "        self.calls.append('hook')\n        return 'moved'\n\n"
        '    def render_seen(self):\n        """A render."""\n'
        "        self.calls.append('render')\n        return {}\n",
        encoding="utf-8")
    loader.load(rig, "orderly")
    calls = registry().get("orderly").backend.calls
    calls.clear()

    body = b'{"config": {"node": {"node001": {"newnodename": "node002"}}}}'
    hooks.after("POST", "config/node/node001", body, 204)
    tasks.drain()
    assert calls == ["hook", "render"], calls


def test_the_last_change_of_a_burst_is_rendered(rig):
    """
    A change inside the coalescing window is rendered when the window closes, never dropped.
    """
    import time as clock
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    seen, state = [], {"value": 1}

    def produce():
        """
        Remember what the render saw.
        """
        seen.append(state["value"])
        return {}

    renders().register("burst", "renders/burst", Render(inputs=["luna.burst"], coalesce="1s"),
                       produce)
    # Registering produces it, so the burst is counted from a clean slate - this test is
    # about what a burst renders, not about what registering does.
    seen.clear()
    renders().notify("luna.burst")
    state["value"] = 2
    renders().notify("luna.burst")
    clock.sleep(1.6)
    assert seen == [1, 2], seen


def test_a_burst_during_a_slow_render_coalesces_into_one_more_pass(rig):
    """
    Two passes for a burst whatever the producer costs, which is what the plugin guide
    promises and what keeps a bulk move over a thousand nodes from costing a render of the
    whole node table per change.

    The window was measured from the render's start while the lock was held across the
    producer, so a producer slower than its own window put every later wake outside it: each
    blocked on the lock, stamped the window with the timestamp it had taken before blocking,
    and then re-rendered in full - and one wake drives four producers.

    Held open on an event and asserted on the number of producer runs, so neither half is a
    race against the clock.
    """
    import threading
    import time as clock

    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    window = 0.4
    runs = []
    arm, started, release = threading.Event(), threading.Event(), threading.Event()

    def produce():
        """
        Hold the burst's first render open until the rest of the burst has been delivered.
        """
        runs.append(1)
        if arm.is_set() and len(runs) == 1:
            started.set()
            release.wait(timeout=10)
        return {}

    engine = renders()
    item = engine.register("slow", "renders/slow",
                           Render(inputs=["luna.slow"], coalesce="1s"), produce)
    # The window in the unit the engine compares it in. A manifest cannot say less than a
    # second, and this test waits out three of these.
    item.coalesce = window
    try:
        runs.clear()
        arm.set()
        first = threading.Thread(target=engine.notify, args=("luna.slow",), daemon=True)
        first.start()
        assert started.wait(timeout=10), "no render ever began, so there is no burst to count"

        # Past the window as the render's start measures it, which is what made each of these
        # a full re-render. They block on the lock until the render above lets go of it.
        clock.sleep(window + 0.2)
        burst = [threading.Thread(target=engine.notify, args=("luna.slow",), daemon=True)
                 for _ in range(4)]
        for thread in burst:
            thread.start()
        clock.sleep(0.2)
        release.set()
        first.join(timeout=10)
        for thread in burst:
            thread.join(timeout=10)

        # Long enough for the trailing pass, and for anything following it to be counted too.
        clock.sleep(window * 3)
        assert len(runs) == 2, \
            f"four changes during one render cost {len(runs)} renders, not the documented two"
    finally:
        release.set()
        engine.forget("slow")


def test_a_render_that_failed_is_not_reported_as_rendered(rig):
    """
    `POST /system/notify` answers what ran as `rendered` and the hash poller logs the same
    list as woken. Both were appended to before the producer was called and its result was
    discarded, so a render that failed was reported as having happened - the same shape as a
    poll that reported itself answering while every fetch errored, one layer up.

    Both halves asserted together: a return that named nothing at all would satisfy the
    negative half on its own.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    def broken():
        """
        A producer with Luna away behind it.
        """
        raise RuntimeError("luna is away")

    def good():
        """
        A producer that answers.
        """
        return {"ok": True}

    engine = renders()
    spec = Render(inputs=["luna.node"], coalesce="0s")
    # These two alone: luna.node is an input of the shipped renders as well, and they would
    # go out to a Luna no unit test has and crowd the answer being asserted.
    engine.forget_all()
    try:
        engine.register("honest", "renders/broken", spec, broken)
        engine.register("honest", "renders/good", spec, good)
        assert engine.notify("luna.node") == ["honest/renders/good"], \
            "a render that failed was reported as rendered, or the one that ran was not"
    finally:
        engine.forget("honest")


def test_a_source_missing_its_library_says_so(rig, monkeypatch):
    """
    A library a real source needs is not a source the ini made up, and the log tells them apart.
    """
    from trixd.core.security import chain
    from trixd.db import engine as database

    real = chain._source

    def without_pam(name, handle):
        """
        The pam source, on a controller where python-pam is not installed.
        """
        if name == "pam":
            raise ModuleNotFoundError("No module named 'pam'", name="pam")
        return real(name, handle)

    said = []
    monkeypatch.setattr(chain, "_source", without_pam)
    monkeypatch.setattr(chain.LOGGER, "error", lambda message, *args: said.append(message % args))
    _local_user("alice", "secret")
    monkeypatch.setattr(configuration.current().auth, "chain", ["pam", "nosuch", "local"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "alice", "secret")
    finally:
        handle.close()
    assert source == "local"
    assert "authentication source pam cannot load: pam is not installed" in said, said
    assert "no authentication source named nosuch; check [AUTH] CHAIN" in said, said


def test_the_cache_is_cleared_after_the_change_is_committed(rig, client, headers, monkeypatch):
    """
    Cleared before the commit, a request in between caches the old row for a minute.
    """
    from trixd.core.security import principals
    from trixd.db import engine as database
    from trixd.db import models

    client.put(f"{API_PREFIX}/identity/users/sealed", json={"password": "pw"},
               headers=headers)
    seen = []
    real = principals.CACHE.clear

    def clear():
        """
        Read the row from another session at the moment the cache is cleared.
        """
        other = database.session()
        try:
            row = other.query(models.Principal).filter(
                models.Principal.name == "sealed").one_or_none()
            seen.append(None if row is None or row.deleted else bool(row.enabled))
        finally:
            other.close()
        real()

    monkeypatch.setattr(principals.CACHE, "clear", clear)
    client.put(f"{API_PREFIX}/identity/users/sealed", json={"enabled": False},
               headers=headers)
    client.delete(f"{API_PREFIX}/identity/users/sealed", headers=headers)
    assert seen == [False, None], seen


def test_the_ini_account_is_not_a_row_the_api_changes(rig, client, headers):
    """
    rootus is set in trixd.ini; the API shows it as rootus and refuses to change or remove it.
    """
    assert client.put(f"{API_PREFIX}/identity/users/tester", json={"role": "manager"},
                      headers=headers).status_code == 400
    assert client.delete(f"{API_PREFIX}/identity/users/tester", headers=headers).status_code == 400
    listed = client.get(f"{API_PREFIX}/identity/users", headers=headers).json()["users"]
    assert [user["role"] for user in listed if user["name"] == "tester"] == ["rootus"]
    assert all("password" not in key for user in listed for key in user if key != "local")


def test_identity_is_three_routes(rig):
    """
    List, set and remove: what obol will call, and nothing else.
    """
    document = rig.openapi()
    routes = sorted((method.upper(), path) for path, item in document["paths"].items()
                    for method in item if path.startswith(f"{API_PREFIX}/identity"))
    assert routes == [("DELETE", f"{API_PREFIX}/identity/users/{{name}}"),
                      ("GET", f"{API_PREFIX}/identity/users"),
                      ("PUT", f"{API_PREFIX}/identity/users/{{name}}")], routes


def test_the_ini_account_signs_in_from_trixd_ini_alone(rig, client, monkeypatch):
    """
    rootus is checked against trixd.ini, whatever the chain lists, leaves no row, and a
    new password in the ini applies at once.
    """
    from trixd.db import engine as database
    from trixd.db import models

    monkeypatch.setattr(configuration.current().auth, "chain", ["pam"])
    login = {"username": "tester", "password": "secret"}
    assert client.post(f"{API_PREFIX}/auth/token", json=login).status_code == 201
    monkeypatch.setattr(configuration.current().api, "password", "changed")
    assert client.post(f"{API_PREFIX}/auth/token", json=login).status_code == 401
    login["password"] = "changed"
    assert client.post(f"{API_PREFIX}/auth/token", json=login).status_code == 201
    handle = database.session()
    try:
        assert handle.query(models.Principal).filter(
            models.Principal.name == "tester").count() == 0
    finally:
        handle.close()


def test_a_deleted_user_comes_back_fresh(rig, client, headers):
    """
    Deleting takes the role and the password; setting the user again starts from none.
    """
    client.put(f"{API_PREFIX}/identity/users/carol", json={"password": "pw", "role": "manager"},
               headers=headers)
    assert client.delete(f"{API_PREFIX}/identity/users/carol", headers=headers).status_code == 200
    login = {"username": "carol", "password": "pw"}
    assert client.post(f"{API_PREFIX}/auth/token", json=login).status_code == 401
    assert client.delete(f"{API_PREFIX}/identity/users/carol", headers=headers).status_code == 404
    shown = client.put(f"{API_PREFIX}/identity/users/carol", json={"password": "pw2"},
                       headers=headers).json()
    assert shown["role"] == "none"


def test_pam_refuses_a_system_account_at_sign_in(rig):
    """
    A system account is refused on a correct password too, not only when resolved.
    """
    from types import SimpleNamespace

    from trixd.core.security.sources import pam as pam_source

    source = pam_source.Source.__new__(pam_source.Source)
    source.service = "login"
    source.pwd = SimpleNamespace(getpwnam=lambda name: SimpleNamespace(
        pw_name=name, pw_uid=2, pw_shell="/sbin/nologin"))
    source.pam = SimpleNamespace(pam=lambda: SimpleNamespace(
        authenticate=lambda *args, **kwargs: True, reason=""))
    verdict, said = source.authenticate("daemon", "right")
    assert verdict is False and "system account" in said


def test_a_unit_name_never_reaches_systemctl_as_an_option(rig, client, headers):
    """
    A catalogue entry that would start with a dash is refused before it is stored.
    """
    catalogue = {"services": {"sneaky": "--root=/tmp"}}
    assert client.patch(f"{API_PREFIX}/services/catalogue", json=catalogue,
                        headers=headers).status_code == 400


def test_settings_that_name_a_command_need_rootus(rig, client, headers):
    """
    A manager changes rules and settings; the command run as root is rootus alone.
    """
    from trixd.core.plugins import manifest as manifests

    for plugin in ("prometheus", "slurm"):
        path = os.path.join(os.path.dirname(trixd.__file__), "plugins", plugin, "manifest.yaml")
        parsed = manifests.parse(manifests.read_yaml(path))
        patched = [e for e in parsed.endpoints if e.path == "config" and "PATCH" in e.methods]
        assert [e.minimum for e in patched] == ["rootus"], plugin


def test_a_bare_duration_is_seconds_except_the_token_expiry(tmp_path):
    """
    CACHE = 20 is twenty seconds; EXPIRY = 2 is two hours, as luna.ini reads it.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[API]\nEXPIRY = 2\n[AUTH]\nCACHE = 20\n", encoding="utf-8")
    loaded = configuration.load(str(ini))
    assert loaded.api.expiry == 7200
    assert loaded.auth.cache == 20
    ini.write_text("[AUTH]\nCACHE = soon\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CACHE"):
        configuration.load(str(ini))


def test_luna_settings_in_trixd_ini_are_ignored_and_named(tmp_path):
    """
    Where Luna answers is Luna's own business and a plugin's settings are the plugin's, so
    both sections still load and are named; the secret beside them is trixd's own and is read.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[TOKENS]\nSECRET_KEY = x\nSECRET_FILE = /nowhere\n"
                   "[LUNA]\nENDPOINT = a:7050\n[TRIGGERS]\nLUNA_HASHES = 60s\n",
                   encoding="utf-8")
    loaded = configuration.load(str(ini))
    assert loaded.ignored == ["[LUNA]", "[TRIGGERS]"]
    assert loaded.tokens.secret_key == "x"


def _loaded(tmp_path, text: str):
    """
    A trixd.ini holding text, loaded, with everything load() found to say about it.

    Taken off the settings object rather than from a handler on a settings logger: the
    ini is read before logging is configured, so a logger there having been called
    proves nothing about anyone seeing it. Where it lands is pinned separately.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text(text, encoding="utf-8")
    loaded = configuration.load(str(ini))
    return loaded, "\n".join(loaded.warnings)


def test_a_max_body_trixd_cannot_read_is_named_rather_than_quietly_the_default(tmp_path):
    """
    A site tightening the ceiling must not end up with a wider one than its ini says: the
    default is still used, but the key, the value and the ceiling in force are all said.
    """
    from trixd.common.constants import DEFAULT_MAX_BODY

    loaded, said = _loaded(tmp_path, "[API]\nMAX_BODY = not a size\n")
    assert "MAX_BODY" in said, "an unreadable ceiling was replaced in silence"
    assert "not a size" in said, "the value that could not be read was not named"
    assert str(DEFAULT_MAX_BODY) in said, "the ceiling actually in force was not named"
    assert loaded.api.max_body == DEFAULT_MAX_BODY, "it must still come up on the default"


@pytest.mark.parametrize("written,expected", [
    ("512KB", 512 * 1024), ("64MB", 64 * 1024 ** 2), ("16 MiB", 16 * 1024 ** 2),
    ("8M", 8 * 1024 ** 2), ("512B", 512), ("1024", 1024), ("0", 0)])
def test_a_ceiling_an_operator_would_write_is_the_ceiling_they_get(tmp_path, written, expected):
    """
    Every natural spelling of a byte count reads as that count. 512KB meaning 8M is the
    whole defect: the 413 that follows names the very setting the site changed.
    """
    loaded, said = _loaded(tmp_path, f"[API]\nMAX_BODY = {written}\n")
    assert loaded.api.max_body == expected, f"{written} did not read as {expected}"
    assert "MAX_BODY" not in said, f"{written} is a spelling trixd should read in silence"


def test_an_ini_with_no_account_says_nobody_can_hold_rootus(tmp_path):
    """
    Empty means no principal holds rootus, so the directory, slurm and replication routes
    answer 403 to everyone and the sweep signs in to its peer as nobody. The daemon still
    comes up, as the luna plugin does with a luna.ini that has no account.
    """
    loaded, said = _loaded(tmp_path, "[API]\nUSERNAME =\nPASSWORD =\n")
    assert "USERNAME" in said and "PASSWORD" in said, "an empty ini account was silent"
    assert "rootus" in said, "the consequence of an empty account was not named"
    assert loaded.api.username == "" and loaded.api.password == ""


@pytest.mark.parametrize("written,expected", [
    ("Y", True), ("enabled", True), ("on", True), ("TRUE", True), ("1", True),
    ("n", False), ("disabled", False), ("off", False), ("False", False), ("0", False)])
def test_replication_reads_the_yes_and_no_an_operator_writes(tmp_path, written, expected):
    """
    ENABLED = Y switching replication off on an HA pair is silent divergence: the outbox
    never drains and the sweep reports no peers where peers are configured.
    """
    loaded, said = _loaded(tmp_path, f"[REPLICATION]\nPEERS = other:7070\nENABLED = {written}\n")
    assert loaded.replication.enabled is expected, f"{written} did not read as {expected}"
    assert "ENABLED" not in said, f"{written} is a spelling trixd should read in silence"


def test_a_replication_switch_trixd_cannot_read_leaves_replication_on(tmp_path):
    """
    Reading an unreadable switch as off is the dangerous guess on a pair, so it is named
    and replication stays on rather than stopping where nobody asked it to.
    """
    loaded, said = _loaded(tmp_path, "[REPLICATION]\nPEERS = other:7070\nENABLED = maybe\n")
    assert loaded.replication.enabled is True, "an unreadable switch must not disable a pair"
    assert "ENABLED" in said and "maybe" in said, "an unreadable switch was silent"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_an_ini_that_cannot_be_read_is_named_rather_than_quietly_the_defaults(tmp_path):
    """
    isfile() passes and RawConfigParser.read() swallows the OSError, so the daemon would
    come up on the default bind, database and plugins while the ini said otherwise.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[DATABASE]\nURL = sqlite:////site/own.db\n", encoding="utf-8")
    ini.chmod(0o000)
    try:
        loaded = configuration.load(str(ini))
    finally:
        ini.chmod(0o644)
    said = "\n".join(loaded.warnings)
    # The precondition first: without this the rest measures a file that was readable.
    assert "own.db" not in loaded.database.url, "the harness did not actually block the read"
    assert loaded.database.url == configuration.DatabaseSettings().url
    assert str(ini) in said, "the file that could not be read was not named"
    assert "ermission" in said, "why it could not be read was not said"


def test_a_key_the_daemon_does_not_read_is_named_one_by_one(tmp_path):
    """
    A misspelled [DATABASE] URL brought the daemon up against a fresh default SQLite file
    with nothing said. The sections trixd ignores on purpose stay quiet.
    """
    loaded, said = _loaded(tmp_path, "[DATABASE]\nURLL = sqlite:////site/own.db\n"
                                     "[NOPE]\nWHAT = ever\n"
                                     "[LUNA]\nENDPOINT = a:7050\n"
                                     "[TRIGGERS]\nLUNA_HASHES = 60s\n")
    # The precondition first: the misspelling really did leave the default in place.
    assert loaded.database.url == configuration.DatabaseSettings().url
    assert loaded.ignored == ["[LUNA]", "[TRIGGERS]"], "the ignored list still names them"
    assert "URLL" in said, "a misspelled key was silent"
    assert "NOPE" in said, "a section trixd does not read was silent"
    assert "LUNA" not in said and "TRIGGERS" not in said, \
        "a section trixd ignores on purpose must not also be complained about"


def test_the_shipped_sample_names_no_key_the_daemon_does_not_read():
    """
    Loading trixd.ini.sample must say nothing: a key the sample explains and the daemon
    never asks for is either a stale sample or a setting that was dropped.
    """
    sample = os.path.join(os.path.dirname(trixd.__file__), "config", "trixd.ini.sample")
    said = "\n".join(configuration.load(sample).warnings)
    assert "does not read" not in said, said


def test_every_setting_the_daemon_reads_is_explained_in_the_shipped_sample(monkeypatch):
    """
    The sample explains each key, and the keys are whatever load() asks the ini for, so
    this is derived from the settings definitions rather than from a list beside them: a
    setting added to load() fails here until the sample names it too.
    """
    sample = os.path.join(os.path.dirname(trixd.__file__), "config", "trixd.ini.sample")
    shipped = configuration.RawConfigParser()
    shipped.read(sample)
    documented = {(name, option) for name in shipped.sections()
                  for option in shipped.options(name)}

    sections, options = set(), set()
    had_section = configuration.RawConfigParser.has_section
    had_option = configuration.RawConfigParser.has_option
    monkeypatch.setattr(configuration.RawConfigParser, "has_section",
                        lambda self, name: (sections.add(name), had_section(self, name))[1])
    monkeypatch.setattr(configuration.RawConfigParser, "has_option",
                        lambda self, name, option: (options.add((name, option.lower())),
                                                    had_option(self, name, option))[1])
    configuration.load(sample)

    skipped = set(getattr(configuration, "IGNORED_SECTIONS", ()))
    assert not sections - skipped - set(shipped.sections()), \
        "the daemon reads a section trixd.ini.sample does not carry"
    assert not options - documented, \
        "the daemon reads a key trixd.ini.sample does not explain"


def test_no_setting_this_daemon_reads_names_luna():
    """
    The daemon's own settings may not know what Luna is. An import of a plugin is caught
    elsewhere; a Luna-named *setting* is not, and one lived here reading an interval that
    belongs to the luna plugin. Enumerated from the model, so the next one is caught too.
    """
    import json

    body = json.dumps(configuration.Settings().model_dump(), default=str).lower()
    assert "luna" not in body, "a setting of this daemon names Luna"
    assert not [name for name in dir(configuration) if "luna" in name.lower()]


def _render_plugin(directory: str, handler: str = "about") -> None:
    """
    A plugin with one served render and one plain endpoint, written to the plugins directory.
    """
    root = os.path.join(directory, "manual")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "manifest.yaml"), "w", encoding="utf-8") as handle:
        handle.write("plugin: manual\nbackend: backend\nendpoints:\n"
                     "  - path: doc\n    methods: [GET]\n    minimum: none\n"
                     "    handler: render_doc\n    render:\n"
                     "      inputs: [luna.node]\n      serve: json\n"
                     "  - path: about\n    methods: [GET]\n    minimum: none\n"
                     f"    handler: {handler}\n")
    with open(os.path.join(root, "backend.py"), "w", encoding="utf-8") as handle:
        handle.write("class Backend:\n"
                     "    def render_doc(self):\n        return {'rendered': True}\n"
                     "    def about(self):\n        return {'about': 'manual'}\n")


def test_a_refused_reload_keeps_the_serving_plugins_renders(rig, client, headers):
    """
    The routes stay and so do the renders behind them: a broken manifest moves nothing.
    """
    from trixd.common.settings import current as settings

    _render_plugin(settings().plugins.directory)
    assert loader.load(rig, "manual").state == "loaded"
    assert client.get(f"{API_PREFIX}/manual/doc", headers=headers).status_code == 200
    _render_plugin(settings().plugins.directory, handler="no_such_method")
    answer = client.post(f"{API_PREFIX}/system/plugins/manual/reload", headers=headers)
    assert answer.status_code == 409
    served = client.get(f"{API_PREFIX}/manual/doc", headers=headers)
    assert served.status_code == 200, served.text


def test_a_reader_reads_the_audit_trail_and_none_does_not(rig, client, headers):
    """
    The read-only user sees who did what; a token with no role does not.
    """
    rita = _principal(client, headers, "rita", "reader")
    nina = _principal(client, headers, "nina")
    client.get(f"{API_PREFIX}/services/catalogue", headers=nina)
    trail = client.get(f"{API_PREFIX}/system/audit", headers=rita)
    assert trail.status_code == 200, trail.text
    rows = trail.json()["audit"]
    assert rows and rows[0]["at"] >= rows[-1]["at"], "newest first"
    assert any(row["principal"] == "nina" and row["outcome"] == "refused" for row in rows)
    assert client.get(f"{API_PREFIX}/system/audit", headers=nina).status_code == 403


def test_the_audit_trail_filters_by_who_outcome_and_time(rig, client, headers):
    """
    Each filter narrows the answer, and a time that is not ISO 8601 is a 400.
    """
    nina = _principal(client, headers, "nina")
    client.get(f"{API_PREFIX}/services/catalogue", headers=nina)
    only = client.get(f"{API_PREFIX}/system/audit", params={"principal": "nina",
                      "outcome": "refused", "limit": 5}, headers=headers).json()["audit"]
    assert only and all(r["principal"] == "nina" and r["outcome"] == "refused" for r in only)
    later = client.get(f"{API_PREFIX}/system/audit", params={"since": "2999-01-01T00:00:00Z"},
                       headers=headers).json()["audit"]
    assert later == []
    assert client.get(f"{API_PREFIX}/system/audit", params={"since": "yesterday"},
                      headers=headers).status_code == 400
    assert client.get(f"{API_PREFIX}/system/audit", params={"outcome": "maybe"},
                      headers=headers).status_code == 422


def test_the_audit_trail_converts_an_offset_in_since(rig, client, headers):
    """
    `Audit.at` is naive UTC, so an offset in `since` has to be converted and not discarded.
    Discarding it filtered from the wall-clock reading instead of the instant, so every row
    in between was silently absent from a 200. The filter test above passes Z, where the two
    readings are the same and the loss cannot show.
    """
    from trixd.db import engine as database
    from trixd.db import models

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    handle = database.session()
    try:
        handle.add(models.Audit(at=now - timedelta(hours=1), principal="offset",
                                action="GET", object="/thing", outcome="granted"))
        handle.commit()
    finally:
        handle.close()

    # Two hours before the row, written at +02:00. Thrown away, the same instant reads as
    # the hour after the row rather than the hour before it.
    since = (now - timedelta(hours=2)).replace(tzinfo=timezone.utc).astimezone(
        timezone(timedelta(hours=2))).isoformat()
    answer = client.get(f"{API_PREFIX}/system/audit",
                        params={"since": since, "principal": "offset"}, headers=headers)
    assert answer.status_code == 200, answer.text
    assert [row["principal"] for row in answer.json()["audit"]] == ["offset"], \
        (f"the row an hour after {since} is not in the answer, so the offset was dropped "
         f"rather than converted: {answer.json()}")


def test_audit_rows_are_written_in_batches_and_none_is_lost(rig):
    """
    A burst of rows lands in the table once the writer has flushed, every one of them.
    """
    from trixd.db import engine as database
    from trixd.db import models

    audit.start()
    for number in range(1200):
        audit.granted("burst", "GET", f"/x/{number}")
    assert audit.flush(10.0)
    handle = database.session()
    try:
        assert handle.query(models.Audit).filter(models.Audit.principal == "burst").count() == 1200
    finally:
        handle.close()


def test_old_audit_rows_are_pruned(rig, monkeypatch):
    """
    Rows older than [AUDIT] KEEP go; recent ones stay.
    """
    from datetime import datetime, timedelta, timezone

    from trixd.db import engine as database
    from trixd.db import models

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=100)
    handle = database.session()
    try:
        handle.add_all([models.Audit(principal="old", action="GET", at=old),
                        models.Audit(principal="new", action="GET")])
        handle.commit()
    finally:
        handle.close()
    assert configuration.current().audit.keep == 90 * 86400
    assert audit.prune() >= 1
    handle = database.session()
    try:
        left = {row.principal for row in handle.query(models.Audit).all()}
    finally:
        handle.close()
    assert "old" not in left and "new" in left


def test_loading_unloading_and_loading_all_are_serialised():
    """
    Two reloads at once must not interleave: each of the three takes the loader's lock.
    """
    for function in (loader.load, loader.unload, loader.load_all):
        assert getattr(function, "__wrapped__", None) is not None, function.__name__


def _broken_plugin(root) -> str:
    """
    A plugin with five things wrong, each a different kind.
    """
    path = root / "broken"
    path.mkdir(parents=True)
    (path / "__init__.py").write_text("", encoding="utf-8")
    (path / "backend.py").write_text(
        "class Backend:\n"
        "    def show(self, name):\n        return {}\n"
        "    def change(self, body=None, extra=None):\n        return {}\n"
        "    def picky(self, wanted):\n        return {}\n", encoding="utf-8")
    (path / "manifest.yaml").write_text(
        "plugin: broken\nbackend: backend\nhooks:\n  luna.node.delete: gone\nendpoints:\n"
        "  - path: item/<node>\n    methods: [GET]\n    minimum: reader\n    handler: show\n"
        "  - path: item/<node>\n    methods: [GET]\n    minimum: reader\n    handler: show\n"
        "  - path: missing\n    methods: [GET]\n    minimum: reader\n    handler: absent\n"
        "  - path: picky\n    methods: [GET]\n    minimum: reader\n    handler: picky\n",
        encoding="utf-8")
    return str(path)


def test_every_shipped_plugin_passes_the_check(rig):
    """
    What ships would load: the check is the gate a new plugin passes too.
    """
    import glob

    from trixd.core.plugins.check import check

    shipped = sorted(os.path.dirname(path) for path in glob.glob(
        os.path.join(os.path.dirname(trixd.__file__), "plugins", "*", "manifest.yaml")))
    assert len(shipped) == 6
    for path in shipped:
        assert check(path) == [], path


def test_the_check_lists_every_problem_at_once(rig, tmp_path):
    """
    An author sees all of them in one run, not one per reload.
    """
    from trixd.core.plugins.check import check

    problems = check(_broken_plugin(tmp_path))
    text = "\n".join(problems)
    assert "GET item/<node> is declared twice" in text
    assert "does not take the path variable node" in text
    assert "names handler absent" in text
    assert "picky needs wanted, which nothing supplies" in text
    assert "luna.node.delete names handler gone" in text


def test_a_handler_that_cannot_take_its_path_variable_is_refused_at_load(rig, tmp_path):
    """
    Refused when it is plugged in, with the reason, rather than a 500 at the first call.
    """
    site = tmp_path / "site-plugins" / "mismatch"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("", encoding="utf-8")
    (site / "backend.py").write_text(
        "class Backend:\n    def show(self, name):\n        return {}\n", encoding="utf-8")
    (site / "manifest.yaml").write_text(
        "plugin: mismatch\nbackend: backend\nendpoints:\n  - path: item/<node>\n"
        "    methods: [GET]\n    minimum: reader\n    handler: show\n", encoding="utf-8")
    with pytest.raises(PluginRefused, match="does not take the path variable node"):
        loader.load(rig, "mismatch", str(site))


def test_a_handler_that_will_only_take_its_variable_positionally_is_refused(rig, tmp_path):
    """
    Every argument is handed over by keyword, so a positional-only parameter is never
    filled: the route bound, the check called the plugin sound, and the first call raised
    before the handler's own first line. The hook branch refuses exactly this shape.
    """
    from trixd.core.plugins.check import check

    site = tmp_path / "site-plugins" / "strict"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("", encoding="utf-8")
    (site / "manifest.yaml").write_text(
        "plugin: strict\nbackend: backend\nendpoints:\n  - path: item/<name>\n"
        "    methods: [GET]\n    minimum: reader\n    handler: show\n", encoding="utf-8")

    def backend(signature: str) -> None:
        """
        A backend whose handler takes its path variable exactly this way.
        """
        (site / "backend.py").write_text(
            f"class Backend:\n    def show(self, {signature}):\n        return {{}}\n",
            encoding="utf-8")

    backend("name, /")
    assert [problem for problem in check(str(site)) if "positionally" in problem], \
        f"the check passed a handler the route cannot call: {check(str(site))}"
    with pytest.raises(PluginRefused, match="positionally"):
        loader.load(rig, "strict", str(site))

    # Not a blanket refusal. The same variable taken by keyword loads, and so does the
    # extra parameter with a default that lets one handler serve an in-process caller too.
    backend('name, was=""')
    assert check(str(site)) == [], "a handler the route can call has to pass the check"
    assert loader.load(rig, "strict", str(site)).state == "loaded"
    loader.unload(rig, "strict")


def test_a_handler_serving_a_read_and_a_write_is_handed_a_body_on_both(rig, client, headers,
                                                                      tmp_path):
    """
    A schema on a GET is kept deliberately, so one handler can serve a read and a write and
    tell them apart by the body being None. The read was handed no body at all rather than
    None, so a handler declaring `body` without a default answered 500 on the read while
    the write answered 200 - and the signature check calls `body` supplied either way.
    """
    site = tmp_path / "site-plugins" / "both"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("", encoding="utf-8")
    (site / "backend.py").write_text(
        "class Backend:\n    def setting(self, body):\n        return {'body': body}\n",
        encoding="utf-8")
    (site / "manifest.yaml").write_text(
        "plugin: both\nbackend: backend\nschemas:\n  value:\n    held: {type: string}\n"
        "endpoints:\n  - path: setting\n    methods: [GET, PATCH]\n    minimum: manager\n"
        "    handler: setting\n    schema: value\n", encoding="utf-8")
    assert loader.load(rig, "both", str(site)).state == "loaded"
    try:
        read = client.get(f"{API_PREFIX}/both/setting", headers=headers)
        assert read.status_code == 200, read.text
        assert read.json()["body"] is None, "the read was handed a body nobody sent"
        written = client.patch(f"{API_PREFIX}/both/setting", headers=headers,
                               json={"held": "a value"})
        assert written.status_code == 200, written.text
        assert written.json()["body"] == {"held": "a value"}
    finally:
        loader.unload(rig, "both")


def test_the_check_command_needs_no_daemon_and_says_why(rig, tmp_path, capsys):
    """
    A directory is checked with no trixd.ini at all; a name is found the way the daemon finds it.
    """
    from trixd import server

    good = os.path.join(os.path.dirname(trixd.__file__), "plugins", "services")
    assert server.main(["plugin", "check", good]) == 0
    assert "would load" in capsys.readouterr().out
    assert server.main(["plugin", "check", _broken_plugin(tmp_path)]) == 1
    assert "declared twice" in capsys.readouterr().out
    config = configuration.current().config_file
    assert server.main(["--config", config, "plugin", "check", "services"]) == 0
    assert server.main(["--config", config, "plugin", "check", "nosuch"]) == 1


def test_the_doorbell_hands_a_change_to_whoever_publishes_that_kind(rig, client, headers):
    """
    Core holds no list of sources: it reads the first part of the event and hands the
    change to whatever registered that name, so a plugin publishing switches.port.down
    works without the daemon learning anything. An event nobody declared is not an
    error - the renders still run - but a source nobody publishes is, because the
    caller would otherwise be told nothing happened and not know why.
    """
    from trixd.core import tasks

    seen = []
    tasks.register("switches", lambda event, param="": seen.append((event, param)) or "noted")
    try:
        answer = client.post(f"{API_PREFIX}/system/notify", headers=headers,
                             json={"event": "switches.port.down", "payload": {"port": "eth0"}})
        assert answer.status_code == 200, answer.text
        assert answer.json()["acted"] == "noted"
        assert seen == [("switches.port.down", '{"port": "eth0"}')]
    finally:
        tasks._handlers.pop("switches", None)

    refused = client.post(f"{API_PREFIX}/system/notify", headers=headers,
                          json={"event": "switches.port.down"})
    assert refused.status_code == 400, "a source nobody publishes must say so"
    assert "switches" in refused.json()["message"]

    shapeless = client.post(f"{API_PREFIX}/system/notify", headers=headers,
                            json={"event": "node.update"})
    assert shapeless.status_code == 400, "two parts is not an event"


def test_the_doorbell_cannot_reach_the_services_worker(rig, client, headers, monkeypatch):
    """
    The services plugin registers its worker for queued actions under the bare name
    `services`, so an event beginning `services.` dispatches straight into it - past the
    action list and the catalogue that `act` applies - and it runs the service command as
    root on the request thread, relaying what the child said.

    What has kept that harmless is the event grammar, which forces two dots into the verb
    so no real action matches: luck, not a check. Nothing reaches the command.
    """
    from trixd.utils import shell

    ran = []

    def fake_run(argv, **kwargs):
        """
        Record the argv instead of running anything.
        """
        ran.append(argv)
        return type("R", (), {"ok": True, "out": "", "err": "", "code": 0})()

    monkeypatch.setattr(shell, "run", fake_run)

    answer = client.post(f"{API_PREFIX}/system/notify", headers=headers,
                         json={"event": "services.unit.restart",
                               "payload": {"name": "slurmctld"}})

    assert ran == [], f"a notify reached the service command: {ran}"
    assert answer.status_code == 400, answer.text


def test_the_doorbell_is_not_the_administrator_alone(client, headers):
    """
    A graphical interface or another tool rings this, not only the daemon's own account.
    An operator may; a reader may not, because it writes files and reloads services.
    """
    reader = _principal(client, headers, "rings-reader", "reader")
    operator = _principal(client, headers, "rings-operator", "operator")

    refused = client.post(f"{API_PREFIX}/system/notify", headers=reader, json={})
    assert refused.status_code == 403, "a reader rang the doorbell"

    allowed = client.post(f"{API_PREFIX}/system/notify", headers=operator, json={})
    assert allowed.status_code == 200, allowed.text
    assert "rendered" in allowed.json()


def test_a_pushed_set_is_queued_and_not_acted_on_inside_the_request(rig, client, headers):
    """
    The difference from the doorbell, and the reason this route exists: acting on a
    change reads back from the source, so a source pushing from inside its own request
    would end up waiting on itself. This answers 202 with the work written down and
    nothing run yet, which is what lets a hook push and carry on.
    """
    from trixd.core import tasks

    acted = []
    tasks.register("switches", lambda event, param="": acted.append(event) or "noted")
    try:
        answer = client.post(f"{API_PREFIX}/system/bulk", headers=headers, json={"changes": [
            {"event": "switches.port.down", "payload": {"port": "eth0"}},
            {"event": "switches.port.up", "payload": {"port": "eth1"}}]})
        assert answer.status_code == 202, answer.text
        queued = answer.json()["queued"]
        assert len(queued) == 2, "one request id per change"

        # The queue row is the evidence, not a stopwatch on the handler: the doorbell
        # dispatches inline and writes no row, so a row existing is what distinguishes
        # the two routes. Asserting the handler had not run yet raced the worker that
        # an earlier test in this file leaves alive, and passed only by luck.
        held = [tasks.status(identifier) for identifier in queued]
        assert [row["task"] for row in held] == ["switches.port.down", "switches.port.up"], \
            "the set must keep the order it was given"
        assert all(row["subsystem"] == "switches" for row in held)
        assert held[0]["param"] == '{"port": "eth0"}'

        tasks.drain()
        assert acted == ["switches.port.down", "switches.port.up"], \
            "the queued work did not run, or ran out of order"
    finally:
        tasks._handlers.pop("switches", None)


def test_a_set_with_one_bad_change_queues_none_of_it(rig, client, headers):
    """
    All or nothing. Half a set is the worst answer available: the caller is told it
    failed and retries, while the accepted half is already being acted on.
    """
    from sqlalchemy import select

    from trixd.core import tasks
    from trixd.db import engine as database
    from trixd.db import models

    tasks.register("switches", lambda event, param="": "noted")
    try:
        refused = client.post(f"{API_PREFIX}/system/bulk", headers=headers, json={"changes": [
            {"event": "switches.port.down", "payload": {"port": "eth0"}},
            {"event": "switches.port", "payload": {"port": "eth1"}}]})
        assert refused.status_code == 400, refused.text
        assert "switches.port" in refused.json()["message"]

        tasks.drain()
        handle = database.session()
        try:
            assert handle.scalars(select(models.Task)).all() == [], \
                "the good half of a refused set was queued anyway"
        finally:
            handle.close()

        empty = client.post(f"{API_PREFIX}/system/bulk", headers=headers, json={"changes": []})
        assert empty.status_code == 400, "an empty set is a caller mistake, not a no-op"
    finally:
        tasks._handlers.pop("switches", None)


def test_unplugging_the_publisher_leaves_the_other_plugins_hooks_dispatching(rig, tmp_path):
    """
    Dispatch is the core's, not the publisher's. Registered by the luna plugin, unplugging
    that plugin took the dispatcher with it and silently stopped every other plugin's hooks
    - and the Prometheus plugin declares hooks on luna.node.update and luna.node.delete, so
    its hardware rules would drift from Luna with nothing anywhere to say so.

    The queue a change runs on is its source, so what is asserted here is that the queue
    still has a handler for `luna` once the luna plugin is gone, and that the handler still
    reaches a plugin that asked for the event.
    """
    from trixd.core import tasks

    plugin = tmp_path / "plugins" / "listener"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: listener\nbackend: backend\nhooks:\n  luna.node.update: moved\nendpoints:\n"
        "  - path: seen\n    methods: [GET]\n    minimum: none\n    handler: seen\n",
        encoding="utf-8")
    (plugin / "backend.py").write_text(
        'class Backend:\n    """A plugin that wants to hear about Luna nodes."""\n'
        "    calls = []\n\n"
        '    def moved(self, name, newnodename="", group=""):\n        """Remember."""\n'
        "        self.calls.append(name)\n        return 'noted'\n\n"
        '    def seen(self):\n        """Show."""\n        return {"calls": self.calls}\n',
        encoding="utf-8")
    loader.load(rig, "listener")
    try:
        # The publisher goes; the subscriber stays.
        loader.unload(rig, "luna")

        dispatcher = tasks.handler("luna")
        assert dispatcher is not None, \
            "no queue answers for luna changes once the luna plugin is unplugged"
        said = dispatcher("luna.node.update", '{"name": "node001", "newnodename": "n2"}')
        assert "listener: noted" in said, said
    finally:
        loader.unload(rig, "listener")


def test_unplugging_the_node_source_makes_a_node_sign_in_refuse(rig, client):
    """
    The node source outlived the plugin that offered it: a node sign-in went on asking an
    unplugged plugin's client whether Luna knows the node, and issuing a token on what it
    said. Refusing because nothing can answer is the honest reply - and it is loud, where
    answering on a stale source is silent.

    Owned the way a trigger is, so the loader lets go of it with everything else.
    """
    from trixd.common.constants import API_PREFIX

    # The reason, not the status: with the plugin loaded this route already answers 503,
    # because the rig's Luna host does not resolve. So what distinguishes the two is what
    # it says, and asserting the status alone would have passed either way.
    before = client.post(f"{API_PREFIX}/auth/node/node001", json={"tpm_sha256": ""})
    assert "node source" not in before.text, before.text

    loader.unload(rig, "luna")
    try:
        after = client.post(f"{API_PREFIX}/auth/node/node001", json={"tpm_sha256": ""})
        assert after.status_code == 503, after.text
        assert "no node source is loaded" in after.text, after.text
    finally:
        loader.load(rig, "luna")


def test_a_body_past_the_ceiling_is_refused_before_anything_reads_it(rig, client, monkeypatch):
    """
    The framework reads the whole body before it solves a route's dependencies, so the gate
    runs after it and cannot bound this. Without a ceiling an unauthenticated caller decides
    how much memory one request costs: a sign-in is public, and the body was read in full
    and only then answered 401.

    Both shapes, because a check on the declared length alone is walked past by not
    declaring one.
    """
    from trixd.common.settings import current as settings

    monkeypatch.setattr(settings().api, "max_body", 2048)

    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "tester", "password": "x" * 4096})
    assert answer.status_code == 413, answer.text
    assert "MAX_BODY" in answer.json()["message"], answer.json()

    def chunks():
        """
        A body that declares no length.
        """
        for _ in range(8):
            yield b"z" * 512

    answer = client.post(f"{API_PREFIX}/auth/token", content=chunks())
    assert answer.status_code == 413, answer.text

    # Still serving: the ceiling refuses the request, it does not refuse the route.
    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "tester", "password": "secret"})
    assert answer.status_code == 201, answer.text

    # A route that reads its own body rather than declaring one - the pass-through to Luna
    # is the only one - reaches the cut as a disconnect, and that exception leaves the
    # middleware before any response is sent. Answered 500 in plain text until it was
    # handled on the way out, which is neither the status the document publishes nor the
    # shape every other refusal uses.
    answer = client.post(f"{API_PREFIX}/luna/config/node/node001", content=chunks())
    assert answer.status_code == 413, f"{answer.status_code}: {answer.text[:120]}"
    assert "MAX_BODY" in answer.json()["message"], answer.json()


def test_a_refused_body_leaves_no_audit_row_to_grow(rig, client, monkeypatch):
    """
    What the ceiling is really for. A failed sign-in writes an audit row whose principal is
    whatever the caller sent, so an unauthenticated caller could put a body-sized value in
    the security record - once per attempt, kept for ninety days. Refused before the handler
    runs, so there is no row at all, and anything that does reach the table is cut to the
    width of its column rather than being offered to the database oversized.
    """
    from trixd.common.settings import current as settings
    from trixd.db import engine as database
    from trixd.db import models

    monkeypatch.setattr(settings().api, "max_body", 2048)
    audit.flush()
    handle = database.session()
    try:
        before = handle.query(models.Audit).count()
    finally:
        handle.close()

    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "n" * 8192, "password": "wrong"})
    assert answer.status_code == 413, answer.text

    audit.flush()
    handle = database.session()
    try:
        assert handle.query(models.Audit).count() == before, "a refused body still wrote a row"
    finally:
        handle.close()

    # And the net underneath it: a value that does reach the trail fits its column, so one
    # oversized row cannot fail the insert and take a whole batch of unrelated rows with it.
    audit.refused("m" * 8192, "GET", "o" * 8192, "testing the width")
    audit.flush()
    handle = database.session()
    try:
        widest = handle.query(models.Audit).order_by(models.Audit.id.desc()).first()
    finally:
        handle.close()
    columns = models.Audit.__table__.columns
    assert len(widest.principal) == columns["principal"].type.length, len(widest.principal)
    assert len(widest.object) == columns["object"].type.length, len(widest.object)


def test_one_failed_task_does_not_end_the_task_worker(rig, monkeypatch):
    """
    One worker, nothing starts a second, and the loop had nothing to catch an exception. So
    anything reaching it ended every queued action for the life of the daemon: submit went
    on answering `queued`, nothing ran it, and the only trace was the interpreter's own
    excepthook rather than a line from this daemon. It recovered on the next restart.

    Driven through the real worker thread, so the thing being pinned is the thread still
    being there afterwards rather than a function returning.
    """
    from trixd.core import tasks as worker

    ran = []

    def handler(task: str, param: str = "") -> str:
        """
        One task, remembered.
        """
        ran.append(task)
        return "ok"

    worker.register("probe", handler)
    assert worker.drain(), "the queue was not empty to begin with, so this proves nothing"

    worker.submit("probe", "before", "", "tester")
    assert worker.drain(), "the worker was not running before the failure"
    assert ran == ["before"], ran

    # Something inside the worker fails in a way the handler's own catch does not cover.
    real = worker._run_one

    def explode(identifier: str) -> None:
        """
        A failure in the worker's own plumbing rather than in a task.
        """
        raise RuntimeError("the database is not there")

    monkeypatch.setattr(worker, "_run_one", explode)
    worker.submit("probe", "during", "", "tester")
    assert worker.drain(), "the worker never let go of the task it could not run"

    monkeypatch.setattr(worker, "_run_one", real)
    worker.submit("probe", "after", "", "tester")
    assert worker.drain(), "the worker is gone: every queued action now waits for a restart"
    assert ran == ["before", "after"], ran


def test_a_session_that_cannot_be_taken_is_said_rather_than_raised(rig, monkeypatch):
    """
    Each of these takes its session outside the try that was meant to hold it, which made
    that one line the only thing in them that could reach the worker loop. The render
    engine's own note-keeping guards the same call for the same reason, so it is exercised
    here too - the docstring used to claim that and the test did not do it, and the engine's
    was the one of the four then guarded that returned in silence.
    """
    import logging

    import trixd

    from trixd.core import tasks as worker
    from trixd.core.plugins.manifest import Render
    from trixd.core.render import engine as rendering
    from trixd.core.render.engine import current as renders
    from trixd.db import engine as database

    def refuse():
        """
        A database that has gone away.
        """
        raise RuntimeError("the database is not there")

    escaped = None

    class Said(logging.Handler):
        """
        Every line these guards emit, kept for the assertion below.
        """

        def __init__(self) -> None:
            """
            An empty list.
            """
            super().__init__(logging.DEBUG)
            self.messages = []

        def emit(self, record) -> None:
            """
            Keep the formatted message.
            """
            self.messages.append(record.getMessage())

    said = Said()
    # Every logger that carries one of these guards, audit's included. Attached to two of
    # the three, audit's line could not reach this collector at all and its entry below was
    # satisfied by another module's message.
    for module in (worker, rendering, audit):
        module.LOGGER.addHandler(said)
        monkeypatch.setattr(module.LOGGER, "level", logging.DEBUG)
    monkeypatch.setattr(database, "session", refuse)
    try:
        # None of these may raise; each says so and returns.
        worker._run_one("whatever")
        worker._prune()
        worker._requeue()

        item = renders().register("noted", "renders/thing",
                                  Render(inputs=["luna.node"], coalesce="0s"),
                                  lambda: {"ok": 1})
        renders()._record(item)
        audit._write([audit._entry("someone", "GET", "/thing", "granted", "")])
        # prune() runs on the writer's own loop with nothing around it, so its session
        # belongs inside the try as the batch write's is. Kept rather than left to fail the
        # run as an error, so the assertion below is what names it.
        try:
            audit.prune()
        except Exception as error:
            escaped = repr(error)
    finally:
        for module in (worker, rendering, audit):
            module.LOGGER.removeHandler(said)
        renders().forget("noted")

    assert escaped is None, \
        (f"audit.prune() let {escaped} out, and the one writer thread it runs on has "
         f"nothing to catch it or restart it")

    # Named rather than counted: registering the render produces it, so the engine's guard
    # fires more than once, and a count would be wrong without saying which.
    for guard in ("to run task", "to prune finished tasks", "to requeue interrupted tasks",
                  "to record render state", "audit row", "to prune the audit trail"):
        matched = [one for one in said.messages if guard in one]
        assert matched, f"the guard for {guard!r} said nothing: {said.messages}"
        assert len(set(matched)) == 1, \
            (f"{guard!r} matches more than one guard's message, so it does not pin its own: "
             f"{sorted(set(matched))}")

    # The list above is of guards, not of threads, so it cannot learn about a fifth worker
    # on its own. The threads themselves are derived: each of these runs for the life of the
    # daemon with nothing to restart it, so one added later has to be considered here rather
    # than quietly going unguarded.
    import ast as syntax

    workers = set()
    for folder, _, files in os.walk(os.path.dirname(trixd.__file__)):
        for name in files:
            if not name.endswith(".py"):
                continue
            tree = syntax.parse(open(os.path.join(folder, name), encoding="utf-8").read())
            for node in syntax.walk(tree):
                if (isinstance(node, syntax.Call) and isinstance(node.func, syntax.Attribute)
                        and node.func.attr == "Thread"):
                    for word in node.keywords:
                        if word.arg == "name" and isinstance(word.value, syntax.Constant):
                            workers.add(word.value.value)
    assert workers == {"trixd-tasks", "trixd-audit", "trixd-sweep", "trixd-hashes"}, \
        (f"the set of long-lived threads changed: {sorted(workers)}. Each runs with nothing "
         f"to restart it, so decide whether the new one's loop survives a failure, then "
         f"update this set")


def test_a_render_replaced_by_a_reload_does_not_produce_afterwards(rig):
    """
    notify swept the render table and then produced from the reference it had taken, so a
    reload landing in between left the unplugged render producing after its replacement had
    already written - and the output left on disk was the old code's. The trailing timer
    looks the render up again under the lock for exactly this reason; the sweep did not.

    Driven rather than raced: the swap is performed from inside the matching check, which is
    the one place guaranteed to run between the sweep taking its reference and the render
    being produced.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    engine = renders()
    ran = []

    def old_producer():
        """
        The render that a reload is about to unplug.
        """
        ran.append("old")
        return {"from": "old"}

    def new_producer():
        """
        What replaces it.
        """
        ran.append("new")
        return {"from": "new"}

    spec = Render(inputs=["luna.node"], coalesce="0s")
    try:
        item = engine.register("swapped", "renders/thing", spec, old_producer)

        swaps = []

        def wants_and_swap(self, changed: str) -> bool:
            """
            Say yes, and swap the plugin's renders while the sweep holds this reference.
            """
            if not swaps:
                swaps.append(1)
                engine.replace("swapped", [("renders/thing", spec, new_producer)])
            return True

        monkeypatch_target = type(item)
        original = monkeypatch_target.wants
        monkeypatch_target.wants = wants_and_swap
        try:
            ran.clear()
            engine.notify("luna.node")
        finally:
            monkeypatch_target.wants = original

        assert swaps == [1], "the swap never happened, so nothing was held across it"
        assert "old" not in ran, \
            f"the unplugged render produced after it was replaced: {ran}"
        assert engine.reader("swapped", "renders/thing")() == {"from": "new"}, \
            "the table is not serving the replacement"
    finally:
        engine.forget("swapped")


def test_a_rename_through_the_pass_through_wakes_the_renders_too(rig, client, headers,
                                                                 monkeypatch):
    """
    A change that queues a hook must also wake the renders that read it. It used to return
    once the hook was queued and leave the renders to the dispatcher, which answers for a
    source only while some loaded plugin declares a hook on it - and one shipped plugin
    declares any. With that plugin disabled the route answered 200, the task failed with
    `nothing handles luna` and nothing logged it, and genders and slurm went on naming the
    node as it was. A change touching no watched field on the same route did re-render,
    which is the wrong way round.

    Driven with no plugin registered for the queue, which is the case that was silent. The
    other case is pinned next door by test_the_renders_wake_after_the_hooks_have_run: where
    a handler does exist the renders are left to it, because a reconcile running beside a
    hook can retire a file that hook is about to move.
    """
    from types import SimpleNamespace

    from trixd.core import tasks
    from trixd.core.render.engine import current as renders

    woken = []
    monkeypatch.setattr(renders(), "notify", lambda changed="": woken.append(changed) or [])

    def luna_says(method, url, **kwargs):
        """
        Luna accepting the rename.
        """
        return SimpleNamespace(content=b"", status_code=204, headers={})

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request", luna_says)

    assert tasks.handler("luna") is None, \
        "a plugin answers for the queue here, so this is the ordered case and not the silent one"
    answer = client.post(f"{API_PREFIX}/luna/config/node/node001",
                         json={"config": {"node": {"node001": {"newnodename": "node002"}}}},
                         headers=headers)
    assert answer.status_code == 204, answer.text

    assert "luna.node" in woken, \
        f"a rename queued its hook and woke no render: {woken}"


def test_a_render_wakes_only_on_the_inputs_it_declares(rig):
    """
    The whole input filter could be stubbed to accept every change with both suites green,
    so nothing held it: a render would run on every change rather than on its own, each
    producer call going out to Luna under the lock every render shares.

    Driven from the shipped manifests rather than invented inputs, and asserting what must
    NOT wake as well as what must - a filter that accepts everything passes any test that
    only checks the positive half.
    """
    import glob as globbing

    import yaml

    from trixd.core.plugins.manifest import Render as Spec
    from trixd.core.render.engine import Render

    declared = {}
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for found in sorted(globbing.glob(os.path.join(here, "trixd/plugins/*/manifest.yaml"))):
        document = yaml.safe_load(open(found, encoding="utf-8"))
        for endpoint in document.get("endpoints") or []:
            if endpoint.get("render"):
                key = f"{document['plugin']}/{endpoint['path']}"
                declared[key] = (document["plugin"],
                                 list(endpoint["render"].get("inputs") or []))
    assert len(declared) >= 4, f"the manifests declared no renders to check: {declared}"

    items = {key: Render(plugin, key.split("/", 1)[1], Spec(inputs=inputs, coalesce="2s"),
                         lambda: None)
             for key, (plugin, inputs) in declared.items()}

    def woken(changed):
        """
        Which shipped renders a change wakes.
        """
        return {key for key, item in items.items() if item.wants(changed)}

    for key, (plugin, inputs) in declared.items():
        for one in inputs:
            named = one if "." in one else f"{plugin}.{one}"
            assert items[key].wants(named), f"{key} declares {one} and does not wake on it"

    # The half a filter that accepts everything fails: a render must not wake on an object
    # it never declared. Taken from what the manifests actually say rather than assumed.
    every = {one if "." in one else f"{plugin}.{one}"
             for plugin, inputs in declared.values() for one in inputs}
    for name in sorted(every):
        for key, (plugin, inputs) in declared.items():
            mine = {one if "." in one else f"{plugin}.{one}" for one in inputs}
            if any(name == one or name.startswith(f"{one}.") or one.startswith(f"{name}.")
                   for one in mine):
                continue
            assert key not in woken(name), \
                f"{key} woke on {name}, which it does not declare and nothing of its does"

    # A change named by its source alone reaches everything under it; one nobody declares
    # reaches nothing.
    assert woken("luna") == {key for key, (_, inputs) in declared.items()
                             if any(str(one).startswith("luna.") for one in inputs)}, \
        "a change named by its source alone did not reach what declares that source"
    assert woken("luna.cluster") == set(), \
        "something declares a cluster input now, so the import decision needs revisiting"


@contextlib.contextmanager
def _listening(logger):
    """
    Every record one logger emits inside the block.

    The daemon's loggers do not propagate, so pytest's own capture never sees them.
    """
    records = []
    handler = logmodule.Handler()
    handler.emit = records.append
    previous = logger.level
    logger.setLevel(logmodule.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _said(records) -> str:
    """
    What a listened-to logger said, as one block of text.
    """
    return "\n".join(record.getMessage() for record in records)


def test_a_refusal_leaves_a_loaded_plugin_serving():
    """
    A reload that refuses must not record the plugin as refused: its routes are still in
    the table and still answering, and the control surface would say otherwise - refused,
    no routes - over a path that answers 200.
    """
    from trixd.common.constants import PLUGIN_STATE_LOADED
    from trixd.core.plugins.registry import LoadedPlugin, Registry

    known = Registry()
    serving = LoadedPlugin("example", PLUGIN_STATE_LOADED)
    serving.routes = ["one live route"]
    known.put(serving)

    after = known.refuse("example", "the new manifest does not parse")
    assert after.state == PLUGIN_STATE_LOADED, "a refusal unplugged what was serving"
    assert after.routes == ["one live route"], "a refusal forgot the routes still bound"
    assert known.get("example") is serving, "a refusal replaced the serving entry"
    assert after.reason == "the new manifest does not parse"
    assert known.listing()[0]["routes"] == 1, "the control surface reports no routes"


def test_a_directory_cannot_take_a_reserved_prefix_through_its_manifest(rig, tmp_path):
    """
    The reserved-name check reads the directory name and the router prefix comes from the
    manifest, so the two have to be made to agree: a plugin directory under any name at
    all would otherwise serve /api/v1/system by saying so in its manifest.
    """
    site = tmp_path / "site-plugins" / "impostor"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("", encoding="utf-8")
    (site / "backend.py").write_text(
        "class Backend:\n    def show(self):\n        return {}\n", encoding="utf-8")
    (site / "manifest.yaml").write_text(
        "plugin: system\nbackend: backend\nendpoints:\n  - path: shadow\n"
        "    methods: [GET]\n    minimum: reader\n    handler: show\n", encoding="utf-8")

    # The route table first, so the proof is the prefix and not only the wording.
    with contextlib.suppress(PluginRefused):
        loader.load(rig, "impostor", str(site))
    assert not _serves(rig, f"{API_PREFIX}/system/shadow"), \
        "a plugin built a route under the prefix the daemon reserves for itself"
    with pytest.raises(PluginRefused, match="calls itself system but sits at impostor"):
        loader.load(rig, "impostor", str(site))


def test_a_manifest_cannot_take_a_service_token():
    """
    `service` is a real token kind, and the gate has no branch for it: it falls through to
    the user path. The minimum requirement is skipped as well, because that fires for a
    user token only, so the route would bind with no role floor and no published access
    line. This refusal is the only thing in the way.
    """
    from trixd.common.constants import TOKEN_KIND_SERVICE

    document = {"plugin": "example", "endpoints": [
        {"path": "thing", "methods": ["GET"], "handler": "show", "minimum": "reader",
         "token": TOKEN_KIND_SERVICE}]}
    with pytest.raises(PluginRefused, match="unknown token kind"):
        parse(document)

    # With no minimum either, to show nothing else would have caught it.
    document["endpoints"][0].pop("minimum")
    with pytest.raises(PluginRefused, match="unknown token kind"):
        parse(document)

    document["endpoints"][0]["token"] = "user"
    with pytest.raises(PluginRefused, match="must declare a minimum role"):
        parse(document)


def test_a_manifest_cannot_bind_a_schema_it_never_declared():
    """
    A typo'd schema name binds the route anyway: the published operation shows no
    requestBody and the handler is handed body: None, so the author's only signal is a
    plugin that quietly ignores what is sent to it.
    """
    document = {"plugin": "example", "schemas": {"setting": {"value": {"type": "string"}}},
                "endpoints": [{"path": "setting", "methods": ["PATCH"], "handler": "setting",
                               "minimum": "manager", "schema": "settings"}]}
    with pytest.raises(PluginRefused, match="wants schema settings, which is not declared"):
        parse(document)

    document["endpoints"][0]["schema"] = "setting"
    assert parse(document).endpoints[0].body_schema == "setting", \
        "the refusal is about the name, not about declaring a schema at all"


def test_a_replicating_endpoint_cannot_also_read():
    """
    Replicate is applied per verb, so a read sharing the endpoint replicates too: every
    GET writes an outbox row, asks the peer, and answers with a replicated block the
    documented example of a read does not have.
    """
    document = {"plugin": "example", "endpoints": [
        {"path": "rules", "methods": ["GET", "PATCH"], "handler": "rules",
         "minimum": "manager", "replicate": True}]}
    with pytest.raises(PluginRefused, match="cannot also serve GET"):
        parse(document)

    document["endpoints"][0]["methods"] = ["PATCH"]
    assert parse(document).endpoints[0].replicate is True, \
        "a write-only endpoint still replicates"


def test_a_schema_on_a_verb_that_takes_no_body_is_refused():
    """
    The binder hands a body to POST, PATCH and PUT only, so a schema on a DELETE is taken
    at parse and dropped at bind: the route and the published document agree with each
    other and not with the manifest, and the signature check cannot see it because `body`
    counts as a known argument whatever the verb.
    """
    document = {"plugin": "example", "schemas": {"rule": {"value": {"type": "string"}}},
                "endpoints": [{"path": "rules/<node>", "methods": ["DELETE"],
                               "handler": "drop", "minimum": "manager", "schema": "rule"}]}
    with pytest.raises(PluginRefused, match="wants schema rule on DELETE, which takes no body"):
        parse(document)

    # GET keeps its schema: a GET and a write on one handler tell a read from a change by
    # the body being None, which is how the scaffold's own example is written.
    document["endpoints"][0]["methods"] = ["GET", "PATCH"]
    assert parse(document).endpoints[0].body_schema == "rule", \
        "a GET sharing a handler with a write lost its schema"


def test_two_paths_differing_only_in_the_variable_name_are_one_route():
    """
    Starlette matches rules/{alpha} for a request to rules/{beta} as well, so the second
    handler is dead - verbatim the harm the collector exists to name - while a key on the
    written path calls them two different routes.
    """
    from trixd.core.plugins.manifest import duplicates

    document = {"plugin": "example", "endpoints": [
        {"path": "rules/<alpha>", "methods": ["GET"], "handler": "first", "minimum": "reader"},
        {"path": "rules/<beta>", "methods": ["GET"], "handler": "second", "minimum": "reader"}]}
    manifest = parse(document)
    assert [endpoint.route_path for endpoint in manifest.endpoints] == [
        "rules/{alpha}", "rules/{beta}"], "both of these are matched by the first route"

    found = duplicates(manifest)
    assert len(found) == 1, f"the dead second handler was not named: {found}"
    assert "rules/<beta>" in found[0] and "rules/<alpha>" in found[0], found[0]

    # A genuinely different path is still two routes, and an exact repeat keeps its wording.
    document["endpoints"][1]["path"] = "ranges/<beta>"
    assert duplicates(parse(document)) == []
    document["endpoints"][1]["path"] = "rules/<alpha>"
    assert duplicates(parse(document)) == ["GET rules/<alpha> is declared twice"]


def test_plugin_state_that_cannot_be_read_says_so(monkeypatch):
    """
    Everything loads when the stored state cannot be read, which is right at a first start
    and wrong every other time: a deliberately unplugged plugin comes back serving. The
    swallow stays; being silent about it does not.
    """
    from trixd.db import engine as database

    class _Handle:
        """
        A session whose first query fails, as it does before the table exists.
        """

        def scalars(self, *_args):
            """
            What SQLAlchemy raises against a database that has no plugin_state yet.
            """
            raise RuntimeError("no such table: plugin_state")

        def close(self):
            """
            Closed either way.
            """

    monkeypatch.setattr(database, "session", _Handle)
    with _listening(loader.LOGGER) as records:
        assert loader._disabled_names() == set(), "the swallow has to stay"
    assert "no such table: plugin_state" in _said(records), \
        "a failed read of the stored state was silent"


def test_a_plugin_in_the_enabled_list_but_not_on_disk_says_so(rig, monkeypatch):
    """
    load_all walks what is on disk, so a name only the ini asks for gets no entry, no
    refusal and no line - unlike the two neighbouring cases, which are both logged.
    """
    import types

    monkeypatch.setattr(loader, "discover", dict)
    monkeypatch.setattr(loader, "_disabled_names", set)
    monkeypatch.setattr(loader, "settings", lambda: types.SimpleNamespace(
        plugins=types.SimpleNamespace(enabled=["nosuch"])))

    with _listening(loader.LOGGER) as records:
        assert loader.load_all(rig) == {}, "a missing plugin must not become an entry"
    assert "nosuch" in _said(records), "a plugin asked for and absent was silent"


def test_an_empty_enabled_list_says_how_many_plugins_it_enabled(rig, monkeypatch):
    """
    An empty [PLUGINS] ENABLED enables every plugin found rather than none, which is the
    opposite of what "a restart loads only what ENABLED lists" reads like. An installed
    site already runs on that, so what is added is the count, not a change of mind.
    """
    import types

    monkeypatch.setattr(loader, "discover", lambda: {"one": "/one", "two": "/two"})
    monkeypatch.setattr(loader, "_disabled_names", set)
    monkeypatch.setattr(loader, "settings", lambda: types.SimpleNamespace(
        plugins=types.SimpleNamespace(enabled=[])))
    monkeypatch.setattr(loader, "load", lambda app, name, path:
                        types.SimpleNamespace(state="loaded"))

    with _listening(loader.LOGGER) as records:
        assert set(loader.load_all(rig)) == {"one", "two"}, \
            "an empty list has always meant every plugin; that is the behaviour here"
    said = _said(records)
    assert "ENABLED" in said, "an empty enabled list enabling everything was silent"
    assert "2" in said, "the number of plugins the empty list enabled was not said"


def test_a_directory_with_no_manifest_is_not_silently_skipped(tmp_path):
    """
    A manifest that is missing or misnamed makes the directory invisible rather than
    refused, which is the one failure an author gets no word about.
    """
    (tmp_path / "halfway").mkdir()
    (tmp_path / "__pycache__").mkdir()

    with _listening(loader.LOGGER) as records:
        assert loader._scan(str(tmp_path)) == {}, "neither directory is a plugin"
    said = _said(records)
    assert "halfway" in said, "a directory with no manifest was skipped in silence"
    assert "__pycache__" not in said, "the interpreter's own directory is not an author's typo"


def test_the_check_refuses_everything_the_loader_refuses_about_a_render(rig, tmp_path):
    """
    An empty list from the check is a promise that the plugin would load. The render branch
    looked for the producer only, so a coalesce the load path refuses passed the check and
    failed on the controller - which is the one place the check exists to come before.
    """
    from trixd.core.plugins.check import check

    path = tmp_path / "slowly"
    path.mkdir()
    (path / "__init__.py").write_text("", encoding="utf-8")
    (path / "backend.py").write_text(
        "class Backend:\n    def render_doc(self):\n        return {}\n", encoding="utf-8")
    (path / "manifest.yaml").write_text(
        "plugin: slowly\nbackend: backend\nendpoints:\n  - path: doc\n    methods: [GET]\n"
        "    minimum: none\n    render:\n      inputs: [luna.node]\n      serve: json\n"
        "      coalesce: soon\n", encoding="utf-8")

    problems = check(str(path))
    assert [problem for problem in problems if "coalesce" in problem], \
        f"the check passed a manifest the loader refuses: {problems}"
    with pytest.raises(PluginRefused, match="coalesce"):
        loader.load(rig, "slowly", str(path))

    (path / "manifest.yaml").write_text(
        (path / "manifest.yaml").read_text(encoding="utf-8").replace("soon", "2s"),
        encoding="utf-8")
    assert check(str(path)) == [], "a render that loads has to pass the check"


def test_every_refusal_on_the_load_path_is_shared_with_the_check():
    """
    An empty list from the check is a promise that the plugin would load, and the promise
    holds only while the load path's refusals run through functions the check calls too. A
    refusal added beside one of them - in the loader's or the binder's own loop rather than
    in the shared validator that loop calls - is reached from the load path alone. That is
    how the render branch's defect came back on the hook branch, and how the binder's pair
    of refusals came to be a second copy that drifted on the wording.

    Derived rather than listed, and over every module of the plugins package rather than
    one of them: an earlier version of this test walked the loader alone and claimed a
    refusal anywhere else failed it by construction, which left the binder's unseen. Each
    function that raises PluginRefused is either reachable from what the check calls, or
    named below as a refusal no check without a daemon could make.

    A refusal is attributed to whatever worded it, which is not always what raises it: a
    raise handing on a list a collector built belongs to the collector, because that is the
    function the check has to call. And a refusal out of reach is excused one at a time, by
    a phrase of its own message, rather than its whole function being excused: exempting
    the function is how two refusals of the binder's sheltered behind an engine guard that
    had nothing to do with them.

    A raise carrying the message of a refusal already made - the load path's own re-raise -
    adds no reason of its own and is counted nowhere.

    What it does not catch: a refusal raised outside this package, and a refusal whose
    wording is built somewhere this reads as neither its own nor a collector's.
    """
    import ast
    import glob

    from trixd.core import plugins as package

    # The refusals a check without a daemon cannot make: where each is worded, a phrase of
    # its message, and why it is out of reach. Nothing else may refuse without the check
    # reaching the function that words it.
    load_only = {
        ("loader", "load", "is reserved"): "the daemon serves that prefix itself, and the "
                                           "check makes its own refusal for the name",
        ("loader", "load", "but sits at"): "the check makes its own refusal for this",
        ("loader", "load", "is already served by"): "a path already taken needs a route "
                                                    "table to be taken in",
        ("loader", "_install", "could not plug itself in"): "plugging in writes to the "
                                                            "registries of a running daemon",
        ("loader", "_from_script", "has no Plugin class"): "a single-file plugin, which the "
                                                           "check never sees: it wants a "
                                                           "manifest.yaml",
        ("loader", "_from_script", "declares no endpoints"): "the same single-file plugin",
        ("binder", "build", "declares a render with no engine"): "refuses its caller rather "
                                                                 "than the plugin: the load "
                                                                 "path always passes one",
    }

    trees, functions, aliases, imported = {}, {}, {}, {}
    for source in sorted(glob.glob(os.path.join(os.path.dirname(package.__file__), "*.py"))):
        module = os.path.basename(source)[:-3]
        with open(source, encoding="utf-8") as handle:
            trees[module] = ast.parse(handle.read())
    for module, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[(module, node.name)] = node
        for node in ast.walk(tree):
            # Both shapes the package imports itself by: the module, under whatever name it
            # is given, and a function out of a sibling module by its own name.
            if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith("trixd"):
                continue
            for alias in node.names:
                if alias.name in trees:
                    aliases[(module, alias.asname or alias.name)] = alias.name
                elif node.module.rsplit(".", 1)[-1] in trees:
                    imported[(module, alias.asname or alias.name)] = (
                        node.module.rsplit(".", 1)[-1], alias.name)

    def _refusals(node) -> list:
        """
        Every PluginRefused raised anywhere under this node.
        """
        return [item for item in ast.walk(node)
                if isinstance(item, ast.Raise) and isinstance(item.exc, ast.Call)
                and getattr(item.exc.func, "id", "") == "PluginRefused"]

    def _calls(module: str, node) -> set:
        """
        The package's own functions this node calls, by either shape.
        """
        found = set()
        for item in ast.walk(node):
            if not isinstance(item, ast.Call):
                continue
            if isinstance(item.func, ast.Name):
                found |= {(module, item.func.id), imported.get((module, item.func.id))}
            elif isinstance(item.func, ast.Attribute) and isinstance(item.func.value, ast.Name):
                found.add((aliases.get((module, item.func.value.id)), item.func.attr))
        return found & set(functions)

    def _literal(exc) -> str:
        """
        The fixed words of one message, which is how a refusal is named above.
        """
        if isinstance(exc, ast.Constant):
            return str(exc.value)
        if isinstance(exc, ast.JoinedStr):
            return " ".join(" ".join(_literal(part).split()) for part in exc.values)
        return ""

    def _wording(module: str, node) -> set:
        """
        Each refusal this function raises: where it was worded - here, or in the collector
        whose list it hands on - and its own fixed words where they are its own.
        """
        collected, kept = {}, {}
        for item in ast.walk(node):
            if (isinstance(item, ast.Assign) and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)):
                for found in _calls(module, item.value):
                    collected[item.targets[0].id] = found
                if _literal(item.value):
                    kept[item.targets[0].id] = _literal(item.value)
        found = set()
        for item in _refusals(node):
            carried = item.exc.args[0] if item.exc.args else None
            held = carried.value if isinstance(carried, ast.Subscript) else carried
            held = held.id if isinstance(held, ast.Name) else ""
            if held in collected:
                found.add((collected[held], ""))
            elif _literal(carried) or kept.get(held):
                found.add(((module, node.name), _literal(carried) or kept[held]))
        return found

    # A refusal outside a function of the module's own is a refusal this walk cannot see.
    for module, tree in trees.items():
        counted = sum(len(_refusals(node)) for (where, _), node in functions.items()
                      if where == module)
        assert counted == len(_refusals(tree)), \
            f"a refusal in {module} sits outside a function of its own, where nothing reads it"

    reached = _calls("check", trees["check"])
    assert reached, "the check calls into the package by name, or this test checks nothing"
    frontier = list(reached)
    while frontier:
        key = frontier.pop()
        for found in _calls(key[0], functions[key]):
            if found not in reached:
                reached.add(found)
                frontier.append(found)

    refusals = set().union(*(_wording(module, node)
                             for (module, _), node in functions.items()))
    assert refusals, "the package refuses something, or this test checks nothing"

    def _excused(where, words) -> list:
        """
        The entries above that name this refusal.
        """
        return [key for key in load_only if key[:2] == where and words and key[2] in words]

    unreachable = sorted(f"{where[0]}.{where[1]} ({words or 'a refusal it hands on'})"
                         for where, words in refusals
                         if where not in reached and not _excused(where, words))
    assert unreachable == [], (
        f"the check cannot reach {', '.join(unreachable)}, so it passes a plugin the load "
        f"path refuses: word the refusal in a function the check calls too, or name it "
        f"above with why a check without a daemon cannot make it")
    stale = sorted(f"{key[0]}.{key[1]} ({key[2]})" for key in load_only
                   if not any(key[:2] == where and key[2] in words for where, words in refusals))
    assert stale == [], (
        f"{', '.join(stale)} refuses nothing any more, so these entries are stale and hide "
        f"whatever takes their place")


def test_the_check_refuses_a_hook_handler_the_dispatcher_could_not_call(rig, tmp_path):
    """
    A change is handed over as `handler(**payload)`, so a parameter that can only be filled
    positionally never is: the loader refuses it, and the check looked for the handler only.
    """
    from trixd.core.plugins.check import check

    path = tmp_path / "picky"
    path.mkdir()
    (path / "__init__.py").write_text("", encoding="utf-8")
    (path / "backend.py").write_text(
        'class Backend:\n    """A plugin whose hook could not be called."""\n\n'
        '    def moved(self, name, /):\n        """Act."""\n        return "noted"\n\n'
        '    def seen(self):\n        """Show."""\n        return {}\n',
        encoding="utf-8")
    (path / "manifest.yaml").write_text(
        "plugin: picky\nbackend: backend\nhooks:\n  luna.node.update: moved\nendpoints:\n"
        "  - path: seen\n    methods: [GET]\n    minimum: none\n    handler: seen\n",
        encoding="utf-8")

    problems = check(str(path))
    assert [problem for problem in problems if "positionally" in problem], \
        f"the check passed a hook the loader refuses: {problems}"
    with pytest.raises(PluginRefused, match="positionally"):
        loader.load(rig, "picky", str(path))


def _noisy_plugin(root) -> None:
    """
    A plugin that registers what runs its tasks as its module is imported, and is refused
    after that import: its hook names a handler the backend does not have.
    """
    plugin = root / "plugins" / "noisy"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: noisy\nbackend: backend\nhooks:\n  luna.node.update: absent\nendpoints:\n"
        "  - path: seen\n    methods: [GET]\n    minimum: none\n    handler: seen\n",
        encoding="utf-8")
    (plugin / "backend.py").write_text(
        "from trixd.core import tasks\n\n\n"
        'class Backend:\n    """A plugin with work of its own."""\n\n'
        '    def seen(self):\n        """Show."""\n        return {}\n\n\n'
        'def run_task(task, param=""):\n    """Run one."""\n    return "noted"\n\n\n'
        'tasks.register("noisy", run_task)\n', encoding="utf-8")


def test_a_refused_load_takes_back_the_task_handler_its_import_registered(rig, tmp_path):
    """
    A plugin registers what runs its tasks as its module is imported, which happens before
    almost every refusal: so a refused plugin went on answering for its own subsystem, and
    a notify naming it was accepted and dispatched into code the loader had just refused.
    """
    import sys

    from trixd.core import tasks

    _noisy_plugin(tmp_path)
    assert tasks.handler("noisy") is None, "nothing answers for noisy before the load"

    with pytest.raises(PluginRefused, match="names handler absent"):
        loader.load(rig, "noisy")

    assert f"{loader.MODULE_PREFIX}noisy.backend" in sys.modules, \
        "the backend was never imported, so nothing registered and this proves nothing"
    assert tasks.handler("noisy") is None, \
        "a refused plugin still answers for its own subsystem"


def test_a_refusal_leaves_hook_dispatch_for_that_source_standing(rig, tmp_path):
    """
    What stood under the plugin's name may be the hook dispatcher rather than a plugin's
    own handler, where another loaded plugin declares a hook on this source. Putting back
    what was found covers that too: dropping the registration instead would stop every
    plugin's hooks on these events, with nothing to say so.
    """
    from trixd.core import hooks, tasks

    watchful = tmp_path / "plugins" / "watchful"
    watchful.mkdir()
    (watchful / "manifest.yaml").write_text(
        "plugin: watchful\nbackend: backend\nhooks:\n  noisy.thing.update: moved\n"
        "endpoints:\n  - path: seen\n    methods: [GET]\n    minimum: none\n"
        "    handler: seen\n", encoding="utf-8")
    (watchful / "backend.py").write_text(
        'class Backend:\n    """A plugin that wants to hear about another one."""\n\n'
        '    def moved(self, name=""):\n        """Act."""\n        return "noted"\n\n'
        '    def seen(self):\n        """Show."""\n        return {}\n',
        encoding="utf-8")
    loader.load(rig, "watchful")
    assert tasks.handler("noisy") is hooks.run, \
        "nothing dispatches noisy hooks, so there is nothing here to lose"

    _noisy_plugin(tmp_path)
    with pytest.raises(PluginRefused, match="names handler absent"):
        loader.load(rig, "noisy")

    assert tasks.handler("noisy") is hooks.run, \
        "the refusal took hook dispatch for noisy events with it"


def test_a_reload_refused_before_the_import_leaves_the_queue_it_found(rig, tmp_path):
    """
    Three refusals come before the backend is imported again - a manifest that cannot be
    read, one that does not parse, a backend that does not import - so at that point the
    handler registered under this plugin's name is still the working one from the load that
    is serving. The routes keep serving on a refusal, and so does the queue behind them: a
    task submitted for this plugin still runs, rather than answering that nothing is
    registered for it.
    """
    from trixd.common.constants import PLUGIN_STATE_LOADED
    from trixd.core import tasks
    from trixd.core.plugins.registry import current as registry

    plugin = tmp_path / "plugins" / "chatty"
    plugin.mkdir()
    sound = ("plugin: chatty\nbackend: backend\nendpoints:\n  - path: seen\n"
             "    methods: [GET]\n    minimum: none\n    handler: seen\n")
    (plugin / "manifest.yaml").write_text(sound, encoding="utf-8")
    (plugin / "backend.py").write_text(
        "from trixd.core import tasks\n\n\n"
        'class Backend:\n    """A plugin with a queue of its own."""\n\n'
        '    def seen(self):\n        """Show."""\n        return {}\n\n\n'
        'def run_task(task, param=""):\n    """Run one."""\n'
        '    return f"chatty ran {task}"\n\n\n'
        'tasks.register("chatty", run_task)\n', encoding="utf-8")

    loader.load(rig, "chatty")
    standing = tasks.handler("chatty")
    assert standing is not None, "the import registered nothing, so this proves nothing"
    assert standing("rebuild") == "chatty ran rebuild", "it answered before the reload"

    # Refused at the parse, which is before the backend is imported again.
    (plugin / "manifest.yaml").write_text(sound.replace("[GET]", "[FROB]"), encoding="utf-8")
    with pytest.raises(PluginRefused, match="unsupported verbs"):
        loader.load(rig, "chatty")

    entry = registry().get("chatty")
    assert entry.state == PLUGIN_STATE_LOADED and entry.routes, \
        "the old plugin stopped serving, so there is no queue here to keep"
    assert tasks.handler("chatty") is standing, \
        "the refused reload unplugged the queue of a plugin that is still serving"
    assert tasks.handler("chatty")("rebuild") == "chatty ran rebuild", \
        "the handler is registered but no longer runs the work"


def test_a_render_has_to_declare_an_input_that_could_wake_it():
    """
    A render is produced once at start-up and then only when an input changes, so a render
    with no inputs, or with one that matches no change there is, serves that first result for
    ever while reporting a healthy render. `inputs` defaults to an empty list, which made the
    silent case the easiest one to write.

    Which object names exist is not checkable here - the engine matches an input against
    <source>.<object>, and those come from Luna's own tables and from a plugin's declaration
    kinds, neither of which anything in the daemon enumerates. The shape is: an input with an
    empty segment can match nothing the engine will ever be handed.
    """
    def document(**render):
        """
        One render endpoint declaring exactly this.
        """
        return {"plugin": "example", "endpoints": [
            {"path": "doc", "methods": ["GET"], "minimum": "reader",
             "render": {"serve": "json", **render}}]}

    assert parse(document(inputs=["luna.node"])).endpoints[0].render.inputs == ["luna.node"]
    assert parse(document(inputs=["settings"])).endpoints[0].render.inputs == ["settings"]

    with pytest.raises(PluginRefused, match="declares no inputs"):
        parse(document())
    with pytest.raises(PluginRefused, match="declares no inputs"):
        parse(document(inputs=[]))
    for unusable in ("", "   ", "luna.", ".node", "luna..node"):
        with pytest.raises(PluginRefused, match="names no change"):
            parse(document(inputs=["luna.node", unusable]))


def test_every_shipped_render_declares_inputs_that_could_wake_it():
    """
    Read from every shipped manifest rather than the ones a test loads, so a render added
    later is covered: it is the render that is never woken again that this catches, and
    nothing at run time reports one.
    """
    import glob
    import os

    import yaml

    found = {}
    for path in glob.glob(os.path.join(os.path.dirname(trixd.__file__), "plugins", "*",
                                       "manifest.yaml")):
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        for endpoint in document["endpoints"]:
            if endpoint.get("render") is not None:
                found[f"{document['plugin']}/{endpoint['path']}"] = endpoint["render"]
    assert found, "no shipped plugin declares a render, so this proves nothing"
    for where, render in found.items():
        inputs = render.get("inputs") or []
        assert inputs, f"{where} declares a render nothing would wake"
        for item in inputs:
            assert all(part.strip() for part in str(item).split(".")), f"{where}: {item}"


def test_a_hook_handler_the_dispatcher_could_not_call_is_refused(rig, tmp_path):
    """
    A change is handed over as `handler(**payload)`, so a parameter that can only be filled
    positionally never is. The load path checked that the attribute was there and nothing
    else, where the binder already refuses an endpoint handler whose signature cannot be
    satisfied - and a hook fails after the caller has its 200, into one log line.
    """
    path = tmp_path / "slanted"
    path.mkdir()
    (path / "__init__.py").write_text("", encoding="utf-8")
    (path / "manifest.yaml").write_text(
        "plugin: slanted\nbackend: backend\nhooks:\n  luna.node.update: on_change\n"
        "endpoints:\n  - path: doc\n    methods: [GET]\n    minimum: reader\n"
        "    handler: show\n", encoding="utf-8")

    def backend(signature: str) -> None:
        """
        A backend whose hook handler takes its name exactly this way.
        """
        (path / "backend.py").write_text(
            "class Backend:\n    def show(self):\n        return {}\n"
            f"    def on_change(self, {signature}):\n        return name\n",
            encoding="utf-8")

    backend("name, /")
    with pytest.raises(PluginRefused, match="positionally"):
        loader.load(rig, "slanted", str(path))

    # Not a blanket refusal: the same handler taking the same field by keyword loads.
    backend("name")
    assert loader.load(rig, "slanted", str(path)).state == "loaded"
    loader.unload(rig, "slanted")


def test_every_shipped_hook_handler_takes_the_payload_its_publisher_sends():
    """
    The payload is splatted into the handler, so a subscriber whose signature does not match
    it raises TypeError on the queue, after the caller has its answer, and nothing else
    reports it. Adding a watched field breaks every existing subscriber the same way.

    Derived from the publisher rather than from a list: the payloads are whatever `notice`
    builds out of the fields it watches, and Python's own binder decides whether a handler
    can take one. A subscriber to an event whose payload cannot be derived here has to take
    `**payload`, because nothing says what it will be sent.
    """
    import glob
    import importlib
    import itertools
    import json
    import os

    import yaml

    from trixd.plugins.luna import hooks as luna_hooks

    # Every payload the pass-through can publish, event by event, from the fields it
    # watches: a rename, a group move, and both at once, plus the delete.
    published = {}
    for count in range(1, len(luna_hooks.WATCHED) + 1):
        for fields in itertools.combinations(luna_hooks.WATCHED, count):
            body = json.dumps({"config": {"node": {"node001": {
                field: f"value-{field}" for field in fields}}}}).encode()
            found = luna_hooks.notice("POST", "config/node/node001", body, 204)
            assert found is not None, f"the publisher sent nothing for {fields}"
            published.setdefault(found["event"], []).append(found["payload"])
    gone = luna_hooks.notice("GET", "config/node/node001/_delete", b"", 204)
    published.setdefault(gone["event"], []).append(gone["payload"])

    wanted = {}
    for path in glob.glob(os.path.join(os.path.dirname(trixd.__file__), "plugins", "*",
                                       "manifest.yaml")):
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        for event, handler in (document.get("hooks") or {}).items():
            module = importlib.import_module(
                f"trixd.plugins.{document['plugin']}.{document.get('backend', 'backend')}")
            wanted[(document["plugin"], event)] = getattr(module.Backend(), handler)
    assert wanted, "no shipped plugin declares a hook, so this proves nothing"

    for (plugin, event), handler in wanted.items():
        accepted = inspect.signature(handler).parameters
        payloads = published.get(event)
        if payloads is None:
            assert any(item.kind == inspect.Parameter.VAR_KEYWORD
                       for item in accepted.values()), \
                (f"{plugin} subscribes to {event}, whose payload is not derivable here, so "
                 f"its handler has to take **payload")
            continue
        for payload in payloads:
            try:
                inspect.signature(handler).bind(**payload)
            except TypeError as error:
                raise AssertionError(
                    f"{plugin}'s handler for {event} cannot take the payload it is sent "
                    f"({sorted(payload)}): {error}") from None
