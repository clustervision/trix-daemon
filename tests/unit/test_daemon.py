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
from datetime import datetime, timedelta, timezone

import jwt
import pytest

import trix

from tests.conftest import SHARED_SECRET
from trix.common import settings as configuration
from trix.common.constants import API_PREFIX, MINIMUMS, RESERVED_PLUGIN
from trix.common.errors import PluginRefused, Unauthenticated
from trix.core.plugins import loader
from trix.core.plugins.manifest import parse
from trix.core.security import tokens


def _serves(app, path: str) -> bool:
    """
    Whether the route table carries this exact path.
    """
    return any(getattr(route, "path", None) == path for route in app.router.routes)


def test_health_needs_no_token(client):
    """
    A health check that needs a token is not a health check.
    """
    answer = client.get(f"{API_PREFIX}/system/health")
    assert answer.status_code == 200
    assert answer.json()["status"] == "ok"


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
    # Luna turns a token away only when it carries a provision scope, so a user
    # token minted here is an administrator there, exactly as its own token is.
    assert claims.get("scope") is None
    assert claims["sub"] == "tester"


def test_a_node_token_carries_lunas_provision_scope(rig):
    """
    Luna confines a provision token to the node it names, so we say which node.
    """
    minted = tokens.mint("node001", kind="node", node="node001")
    claims = jwt.decode(minted, SHARED_SECRET, algorithms=["HS256"])
    assert claims["scope"] == "provision"
    assert claims["node"] == "node001"


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
    assert header["kid"] == "trix-test"
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


def test_every_user_endpoint_declares_a_minimum_role(rig):
    """
    Derived from the loaded manifests: it fails on the first one that forgets.
    """
    from trix.core.plugins.registry import current as registry
    for entry in registry().plugins.values():
        if entry.manifest is None:
            continue
        for endpoint in entry.manifest.endpoints:
            if endpoint.public or endpoint.token == "node":
                continue
            assert endpoint.minimum in MINIMUMS, f"{entry.name}/{endpoint.path} declares none"


def test_the_published_document_matches_the_route_table(rig):
    """
    Enumerate one, assert the other.
    """
    document = rig.openapi()
    published = set(document["paths"])
    registered = {route.path for route in rig.router.routes
                  if getattr(route, "include_in_schema", False)
                  and getattr(route, "path", "").startswith(API_PREFIX)}
    assert registered - published == set()


def test_luna_is_reserved(rig):
    """
    No plugin may claim the one-API route to Luna.
    """
    with pytest.raises(PluginRefused) as raised:
        loader.load(rig, RESERVED_PLUGIN)
    assert RESERVED_PLUGIN in str(raised.value)


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
        if not path.startswith(API_PREFIX) or RESERVED_PLUGIN in path:
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

    root = pathlib.Path(trix.__file__).parent / "core" / "security" / "sources"
    found = {path.stem for path in root.glob("*.py") if path.stem != "__init__"}
    assert found == {"local", "pam", "ldap", "oidc"}, found


def test_no_source_authenticates_without_checking_the_password(rig):
    """
    Every source must refuse or decline; none may answer True on the name alone.
    """
    import pathlib

    root = pathlib.Path(trix.__file__).parent / "core" / "security" / "sources"
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


def test_a_source_that_cannot_work_is_skipped(rig, monkeypatch):
    """
    A missing library must not stop a local principal from logging in.
    """
    from trix.core.security import chain
    from trix.db import engine as database

    monkeypatch.setattr(configuration.current().auth, "chain", ["ldap", "local"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "tester", "secret")
    finally:
        handle.close()
    assert source == "local"
    assert identity["name"] == "tester"


def test_a_refusal_stops_the_chain(rig, monkeypatch):
    """
    A source that knows the name and refuses it is the end of the walk.
    """
    from trix.core.security import chain
    from trix.db import engine as database

    monkeypatch.setattr(configuration.current().auth, "chain", ["local", "pam"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "tester", "wrong")
    finally:
        handle.close()
    assert identity is None
    assert source == "local"


def test_an_unknown_source_name_is_skipped(rig, monkeypatch):
    """
    A typo in the chain is logged, not a crash and not an open door.
    """
    from trix.core.security import chain
    from trix.db import engine as database

    monkeypatch.setattr(configuration.current().auth, "chain", ["nosuchsource"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "tester", "secret")
    finally:
        handle.close()
    assert identity is None
    assert source == ""


def test_a_node_token_is_not_issued_on_the_node_name_alone(rig, client, monkeypatch):
    """
    Luna never issues one anonymously, and the token it mints works on Luna too.
    """
    from trix.api.v1 import auth as auth_routes

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

    monkeypatch.setattr(auth_routes, "luna", lambda: Stub())
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
    from trix.api.v1 import auth as auth_routes

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

    monkeypatch.setattr(auth_routes, "luna", lambda: Stub())
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
    from trix.plugins.services import backend as services

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
    from trix.db import engine as database
    from trix.db import models

    client.post(f"{API_PREFIX}/auth/token",
                json={"username": "tester", "password": "secret"})
    client.post(f"{API_PREFIX}/auth/token",
                json={"username": "tester", "password": "wrong"})

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
    from trix.db import engine as database
    from trix.db import models

    node_token = tokens.mint("node001", kind="node", node="node001")
    client.get(f"{API_PREFIX}/directory/users", headers={"x-access-tokens": node_token})

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
    from trix.db import engine as database
    from trix.db import models

    # create_principal flushes before it audits, so the request holds the write
    # lock: a second session here waits out the busy timeout and loses the row.
    client.post(f"{API_PREFIX}/identity/principals", json={"name": "audited"},
                headers=headers)
    handle = database.session()
    try:
        rows = handle.query(models.Audit).filter(
            models.Audit.object == "principal/audited").all()
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
    body = {"name": name, "password": "pw"}
    if role is not None:
        body["role"] = role
    answer = client.post(f"{API_PREFIX}/identity/principals", json=body, headers=headers)
    assert answer.status_code == 201, answer.text
    return _as(client, name, "pw")


def test_no_role_may_look_and_not_change(rig, client, headers):
    """
    The bottom of the ladder holds a token and reads, which is what an opened app needs.
    """
    rita = _principal(client, headers, "rita")
    assert client.get(f"{API_PREFIX}/services/catalogue", headers=rita).status_code == 200
    refused = client.patch(f"{API_PREFIX}/services/catalogue", json={}, headers=rita)
    assert refused.status_code == 403, refused.text
    assert "manager" in refused.text and "none" in refused.text


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
    assert client.patch(f"{API_PREFIX}/services/catalogue", json={},
                        headers=mo).status_code in (200, 422)


def test_rootus_is_checked_against_nothing(rig, client, headers):
    """
    The account the ini names holds no role and needs none.
    """
    assert client.patch(f"{API_PREFIX}/services/catalogue", json={},
                        headers=headers).status_code in (200, 422)
    said = client.get(f"{API_PREFIX}/auth/whoami", headers=headers).json()
    assert said["rootus"] is True
    assert said["role"] == "none"


def test_a_role_outside_the_ladder_is_refused(rig, client, headers):
    """
    An unknown role would rank at the bottom and look like a working account.
    """
    answer = client.post(f"{API_PREFIX}/identity/principals",
                         json={"name": "king", "role": "king"}, headers=headers)
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
    from trix.db import engine as database
    from trix.db import models

    pat = _principal(client, headers, "pat")
    client.patch(f"{API_PREFIX}/services/catalogue", json={}, headers=pat)
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
    The one table RBAC has: a role is given on creation and changed in place.
    """
    carol = _principal(client, headers, "carol", "operator")
    shown = client.get(f"{API_PREFIX}/identity/principals/carol", headers=headers).json()
    assert shown["role"] == "operator"
    assert client.patch(f"{API_PREFIX}/identity/principals/carol", json={"role": "manager"},
                        headers=headers).status_code == 200
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=carol).json()["role"] == "manager"


def test_the_oidc_source_lets_the_chain_continue(rig):
    """
    Named before it is built: it must never claim a name, or nobody could log in.
    """
    from trix.core.security.sources import oidc

    source = oidc.Source()
    assert source.authenticate("tester", "secret")[0] is None
    assert source.resolve("tester")[0] is False


def test_a_rootus_minimum_admits_the_ini_account_only(rig, client, headers):
    """
    The top of the ladder is not a role: a manager is refused where Luna wants rootus.
    """
    mo = _principal(client, headers, "mo", "manager")
    refused = client.get(f"{API_PREFIX}/identity/principals", headers=mo)
    assert refused.status_code == 403 and "rootus" in refused.text, refused.text
    assert client.get(f"{API_PREFIX}/system/replication/hashes", headers=mo).status_code == 403
    assert client.get(f"{API_PREFIX}/identity/principals", headers=headers).status_code == 200


def test_rootus_cannot_be_given_as_a_role(rig, client, headers):
    """
    It is the ini account by name, never a row, so the API refuses to store it.
    """
    answer = client.post(f"{API_PREFIX}/identity/principals",
                         json={"name": "pretender", "role": "rootus"}, headers=headers)
    assert answer.status_code == 400


def test_a_deleted_principal_is_refused_at_once(rig, client, headers):
    """
    The token stays valid; the row is gone, so the next request cannot resolve it.
    """
    frank = _principal(client, headers, "frank")
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=frank).status_code == 200
    assert client.delete(f"{API_PREFIX}/identity/principals/frank",
                         headers=headers).status_code == 200
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=frank).status_code == 401


def test_the_ladder_is_ordered_and_unknown_sits_at_the_bottom(rig):
    """
    none, operator, manager, in that order; a role nobody declared ranks as none.
    """
    from trix.core.security import access

    assert access.rank("none") < access.rank("operator") < access.rank("manager")
    assert access.rank("manager") < access.rank("rootus")
    assert access.rank("king") == access.rank("none")
    assert access.permits(None, "none") == (True, "none")
    assert access.permits(None, "operator") == (False, "none")
    assert access.permits(None, "rootus") == (False, "none")
    assert access.permits(None, "rootus", "service") == (True, "service")


def test_a_service_action_is_queued_and_polled(rig, client, headers, monkeypatch):
    """
    The thirteenth route: Luna answers an action with an id and you poll it.
    """
    from trix.core import tasks
    from trix.plugins.services import backend as services

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
    from trix.common.errors import BackendFailure
    from trix.core import tasks

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
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "cut", "password": "pw"}, headers=headers)
    theirs = _as(client, "cut", "pw")
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=theirs).status_code == 200

    client.patch(f"{API_PREFIX}/identity/principals/cut",
                 json={"enabled": False}, headers=headers)
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=theirs).status_code == 401

    client.patch(f"{API_PREFIX}/identity/principals/cut",
                 json={"enabled": True}, headers=headers)
    assert client.get(f"{API_PREFIX}/auth/whoami", headers=theirs).status_code == 200


def test_a_disabled_principal_cannot_log_in_again(rig, client, headers):
    """
    And the source refuses the password too, not only the resolve.
    """
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "barred", "password": "pw", "enabled": False},
                headers=headers)
    answer = client.post(f"{API_PREFIX}/auth/token",
                         json={"username": "barred", "password": "pw"})
    assert answer.status_code == 401


def test_a_requeued_task_finds_its_handler(rig):
    """
    Starting the worker before the plugins register fails the work it recovers.
    """
    from trix.core import lifespan
    from trix.core.plugins import loader
    from trix.core import tasks

    source = inspect.getsource(lifespan.start)
    assert source.index("loader.load_all") < source.index("tasks.start()"), \
        "the worker must start after the plugins, or a requeued task has no handler"


def test_the_pass_through_recognises_a_node_rename(rig):
    """
    A POST to the node with newnodename or group, answered 2xx, is what a hook must see.
    """
    from trix.core.luna import hooks

    body = b'{"config": {"node": {"node001": {"newnodename": "node002", "comment": "x"}}}}'
    assert hooks.notice("POST", "config/node/node001", body, 204) == {
        "event": "node.update", "payload": {"name": "node001", "newnodename": "node002"}}
    moved = b'{"config": {"node": {"node001": {"group": "gpu"}}}}'
    assert hooks.notice("POST", "config/node/node001", moved, 204) == {
        "event": "node.update", "payload": {"name": "node001", "group": "gpu"}}
    assert hooks.notice("GET", "config/node/node001/_delete", b"", 204) == {
        "event": "node.delete", "payload": {"name": "node001"}}

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
    from trix.core.luna import proxy
    from trix.db import engine as database
    from trix.db import models

    def luna_says(method, url, **kwargs):
        """
        Luna accepting the rename with an empty 204.
        """
        return SimpleNamespace(content=b"", status_code=204, headers={})

    monkeypatch.setattr(proxy.requests, "request", luna_says)
    answer = client.post(f"{API_PREFIX}/luna/config/node/node001",
                         json={"config": {"node": {"node001": {"newnodename": "node002"}}}},
                         headers=headers)
    assert answer.status_code == 204

    handle = database.session()
    try:
        row = handle.query(models.Task).filter(models.Task.subsystem == "luna").one()
        assert row.task == "node.update" and '"newnodename": "node002"' in row.param
    finally:
        handle.close()


def test_a_plugin_declares_the_changes_it_acts_on(rig, tmp_path, client, headers):
    """
    The manifest names the event and the handler; the dispatcher hands the payload to it.
    """
    from trix.core.luna import hooks

    plugin = tmp_path / "plugins" / "watcher"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: watcher\nbackend: backend\nluna_hooks:\n  node.update: moved\nendpoints:\n"
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

    said = hooks.run("node.update", '{"name": "node001", "newnodename": "node002"}')
    assert said == "watcher: noted"
    shown = client.get(f"{API_PREFIX}/watcher/seen", headers=headers).json()
    assert shown["calls"] == [["node001", "node002", ""]]
    assert hooks.run("node.delete", '{"name": "node001"}') == "nothing declared node.delete"


def test_a_hook_for_an_unknown_change_is_refused():
    """
    A plugin cannot subscribe to something the pass-through never notices.
    """
    with pytest.raises(Exception) as raised:
        parse({"plugin": "x", "luna_hooks": {"node.paint": "paint"}, "endpoints": [
            {"path": "a", "methods": ["GET"], "minimum": "none", "handler": "a"}]})
    assert "node.paint" in str(raised.value)


def test_the_renders_wake_after_the_hooks_have_run(rig, tmp_path):
    """
    A reconcile woken beside the hook could retire a file the hook was about to move.
    """
    from trix.core import tasks
    from trix.core.luna import hooks
    from trix.core.plugins.registry import current as registry

    plugin = tmp_path / "plugins" / "orderly"
    plugin.mkdir()
    (plugin / "manifest.yaml").write_text(
        "plugin: orderly\nbackend: backend\nluna_hooks:\n  node.update: moved\nendpoints:\n"
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
    from trix.core.plugins.manifest import Render
    from trix.core.render.engine import current as renders

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
    from trix.core.security import chain
    from trix.db import engine as database

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
    monkeypatch.setattr(configuration.current().auth, "chain", ["pam", "nosuch", "local"])
    handle = database.session()
    try:
        identity, source = chain.authenticate(handle, "tester", "secret")
    finally:
        handle.close()
    assert source == "local"
    assert "authentication source pam cannot load: pam is not installed" in said, said
    assert "no authentication source named nosuch; check [AUTH] CHAIN" in said, said


def test_the_cache_is_cleared_after_the_change_is_committed(rig, client, headers, monkeypatch):
    """
    Cleared before the commit, a request in between caches the old row for a minute.
    """
    from trix.core.security import principals
    from trix.db import engine as database
    from trix.db import models

    client.post(f"{API_PREFIX}/identity/principals", json={"name": "sealed", "password": "pw"},
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
            seen.append(None if row is None else bool(row.enabled))
        finally:
            other.close()
        real()

    monkeypatch.setattr(principals.CACHE, "clear", clear)
    client.patch(f"{API_PREFIX}/identity/principals/sealed", json={"enabled": False},
                 headers=headers)
    client.delete(f"{API_PREFIX}/identity/principals/sealed", headers=headers)
    assert seen == [False, None], seen
