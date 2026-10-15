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

import inspect
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


def test_no_source_authenticates_without_checking_the_password(rig):
    """
    Every source must refuse or decline; none may answer True on the name alone.
    """
    import pathlib

    root = pathlib.Path(trixd.__file__).parent / "core" / "security" / "sources"
    for name in ("local", "pam", "ldap", "oidc"):
        body = (root / f"{name}.py").read_text(encoding="utf-8")
        block = body.split("def authenticate")[1].split("\n    def ")[0]
        # Drop the signature and the docstring: both name the password without using
        # it, and a check that counts them passes on a source that ignores it.
        code = block.split('"""')[2] if block.count('"""') >= 2 else block
        if "return True" not in code:
            continue
        before = code.split("return True")[0]
        assert "password" in before, f"{name} answers True without using the password"


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
    assert access.permits(None, "rootus", "service") == (True, "service")


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


def test_a_plugin_declares_the_changes_it_acts_on(rig, tmp_path, client, headers):
    """
    The manifest names the event and the handler; the dispatcher hands the payload to it.
    """
    from trixd.plugins.luna import hooks

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
    renders().notify("luna.burst")
    state["value"] = 2
    renders().notify("luna.burst")
    clock.sleep(1.6)
    assert seen == [1, 2], seen


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
