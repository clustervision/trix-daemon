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

from datetime import datetime, timedelta, timezone

import jwt
import pytest

import trix

from tests.conftest import SHARED_SECRET
from trix.common import settings as configuration
from trix.common.constants import API_PREFIX, RESERVED_PLUGIN
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


def test_every_plugin_endpoint_declares_a_capability(rig):
    """
    Derived from the loaded manifests: it fails on the first one that forgets.
    """
    from trix.core.plugins.registry import current as registry
    for entry in registry().plugins.values():
        if entry.manifest is None:
            continue
        for endpoint in entry.manifest.endpoints:
            assert endpoint.capability, f"{entry.name}{endpoint.path} declares none"


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


def test_a_manifest_without_a_capability_is_refused():
    """
    An endpoint that names no capability would be an open door.
    """
    with pytest.raises(Exception) as raised:
        parse({"plugin": "x", "endpoints": [{"path": "a", "methods": ["GET"],
                                             "handler": "a"}]})
    assert "capability" in str(raised.value)


def test_a_manifest_naming_a_missing_handler_is_refused(rig, tmp_path):
    """
    Half a plugin is worse than none, so the whole plugin is refused.
    """
    broken = tmp_path / "plugins" / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text(
        "plugin: broken\nbackend: backend\nendpoints:\n"
        "  - path: a\n    methods: [GET]\n    capability: broken.read\n"
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


def test_no_source_authenticates_without_checking_the_password(rig):
    """
    Every source must refuse or decline; none may answer True on the name alone.
    """
    import pathlib

    root = pathlib.Path(trix.__file__).parent / "core" / "security" / "sources"
    for name in ("local", "pam", "ldap", "directory"):
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


def test_the_rbac_store_can_be_filled_over_the_api(rig, client, headers):
    """
    The schema existed and nothing could populate it, so RBAC was unreachable.
    """
    assert client.post(f"{API_PREFIX}/identity/usergroups",
                       json={"name": "intel"}, headers=headers).status_code == 201
    assert client.post(f"{API_PREFIX}/identity/principals",
                       json={"name": "carol", "password": "x"},
                       headers=headers).status_code == 201
    answer = client.post(f"{API_PREFIX}/identity/usergroups/intel/members",
                         json={"principal": "carol", "role": "operator"},
                         headers=headers)
    assert answer.status_code == 201, answer.text
    assert answer.json()["role"] == "operator"

    shown = client.get(f"{API_PREFIX}/identity/principals/carol", headers=headers).json()
    assert shown["usergroups"] == {"intel": "operator"}
    assert shown["admin"] is False


def test_a_role_outside_the_four_is_refused(rig, client, headers):
    """
    An unknown role would cap to nothing and look like a working membership.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "amd"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/principals", json={"name": "dave"}, headers=headers)
    answer = client.post(f"{API_PREFIX}/identity/usergroups/amd/members",
                         json={"principal": "dave", "role": "superuser"}, headers=headers)
    assert answer.status_code == 400
    assert "superuser" in answer.text


def test_a_second_add_sets_the_role_rather_than_failing(rig, client, headers):
    """
    Adding twice is how a role is changed, so it must not collide on the unique key.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "ops"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/principals", json={"name": "erin"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/usergroups/ops/members",
                json={"principal": "erin", "role": "reader"}, headers=headers)
    answer = client.post(f"{API_PREFIX}/identity/usergroups/ops/members",
                         json={"principal": "erin", "role": "admin"}, headers=headers)
    assert answer.status_code == 201
    shown = client.get(f"{API_PREFIX}/identity/principals/erin", headers=headers).json()
    assert shown["usergroups"] == {"ops": "admin"}


def test_deleting_a_principal_takes_its_memberships(rig, client, headers):
    """
    Nothing may be left pointing at a row that is gone.
    """
    from trix.db import engine as database
    from trix.db import models

    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "temp"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/principals", json={"name": "frank"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/usergroups/temp/members",
                json={"principal": "frank", "role": "reader"}, headers=headers)
    assert client.delete(f"{API_PREFIX}/identity/principals/frank",
                         headers=headers).status_code == 200

    handle = database.session()
    try:
        assert handle.query(models.Membership).count() == 0
    finally:
        handle.close()


def test_an_external_group_maps_onto_a_usergroup_and_a_role(rig, client, headers):
    """
    Two directory groups may feed one usergroup with different roles.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "hpc"}, headers=headers)
    for group, role in (("cn=hpc-admins", "admin"), ("cn=hpc-users", "reader")):
        answer = client.post(f"{API_PREFIX}/identity/usergroupmap",
                             json={"source": "ldap", "external_group": group,
                                   "usergroup": "hpc", "role": role}, headers=headers)
        assert answer.status_code == 201, answer.text

    rows = client.get(f"{API_PREFIX}/identity/usergroupmap", headers=headers).json()
    roles = {row["external_group"]: row["role"] for row in rows["usergroupmap"]}
    assert roles == {"cn=hpc-admins": "admin", "cn=hpc-users": "reader"}


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


def test_a_reader_may_look_and_not_change(rig, client, headers):
    """
    The role caps the class bits, which is the whole point of having a role.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "obs"},
                headers=headers)
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "rita", "password": "pw"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/usergroups/obs/members",
                json={"principal": "rita", "role": "reader"}, headers=headers)
    # List the usergroup against the plugin, or rita is merely other.
    client.patch(f"{API_PREFIX}/system/acl/services",
                 json={"usergroups": "obs"}, headers=headers)

    rita = _as(client, "rita", "pw")
    assert client.get(f"{API_PREFIX}/services/catalogue", headers=rita).status_code == 200
    refused = client.patch(f"{API_PREFIX}/services/catalogue", json={}, headers=rita)
    assert refused.status_code == 403, refused.text
    assert "w" in refused.text


def test_a_manager_may_change_what_a_reader_may_not(rig, client, headers):
    """
    Same object, same bits, different role: that is the axis the design turns on.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "eng"},
                headers=headers)
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "mo", "password": "pw"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/usergroups/eng/members",
                json={"principal": "mo", "role": "manager"}, headers=headers)
    client.patch(f"{API_PREFIX}/system/acl/services",
                 json={"usergroups": "eng"}, headers=headers)

    mo = _as(client, "mo", "pw")
    assert client.get(f"{API_PREFIX}/services/catalogue", headers=mo).status_code == 200
    assert client.patch(f"{API_PREFIX}/services/catalogue", json={},
                        headers=mo).status_code in (200, 422)


def test_someone_outside_every_usergroup_is_other(rig, client, headers):
    """
    The default mode gives other nothing, so an outsider sees nothing.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "inside"},
                headers=headers)
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "olive", "password": "pw"}, headers=headers)

    olive = _as(client, "olive", "pw")
    assert client.get(f"{API_PREFIX}/services/catalogue", headers=olive).status_code == 403


def test_the_admin_flag_is_checked_against_nothing(rig, client, headers):
    """
    A day-to-day superuser, named in the audit trail rather than sharing rootus.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "any"},
                headers=headers)
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "sam", "password": "pw", "admin": True}, headers=headers)

    sam = _as(client, "sam", "pw")
    assert client.get(f"{API_PREFIX}/services/catalogue", headers=sam).status_code == 200


def test_nothing_is_enforced_until_somebody_is_organised(rig, client, headers):
    """
    An upgraded cluster with no usergroups behaves exactly as it did before.
    """
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "pat", "password": "pw"}, headers=headers)
    pat = _as(client, "pat", "pw")
    assert client.get(f"{API_PREFIX}/services/catalogue", headers=pat).status_code == 200


def test_whoami_reports_the_role_in_each_usergroup(rig, client, headers):
    """
    A role is what a person can be told; a bit list is not.
    """
    client.post(f"{API_PREFIX}/identity/usergroups", json={"name": "team"},
                headers=headers)
    client.post(f"{API_PREFIX}/identity/principals",
                json={"name": "wendy", "password": "pw"}, headers=headers)
    client.post(f"{API_PREFIX}/identity/usergroups/team/members",
                json={"principal": "wendy", "role": "manager"}, headers=headers)

    said = client.get(f"{API_PREFIX}/auth/whoami",
                      headers=_as(client, "wendy", "pw")).json()
    assert said["principal"] == "wendy"
    assert said["usergroups"] == {"team": "manager"}
    assert said["admin"] is False
    assert said["source"] == "local"


def test_a_mode_is_accepted_as_octal_or_as_letters(rig, client, headers):
    """
    The API takes rwxrwx--- as chmod does, and stores the octal the tree uses.
    """
    answer = client.patch(f"{API_PREFIX}/system/acl/services",
                          json={"access": "rwxr-x---"}, headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.json()["access"] == "750"
    assert answer.json()["mode"] == "rwxr-x---"

    answer = client.patch(f"{API_PREFIX}/system/acl/services",
                          json={"access": "770"}, headers=headers)
    assert answer.json()["mode"] == "rwxrwx---"
    assert client.patch(f"{API_PREFIX}/system/acl/services",
                        json={"access": "nonsense"}, headers=headers).status_code == 400


def test_an_action_post_needs_operate_not_change(rig, client, headers):
    """
    Restarting a service is operating it, which is x, not w.
    """
    from trix.core.security import access

    assert access.bit_for("GET", "/api/v1/services/catalogue") == "r"
    assert access.bit_for("PATCH", "/api/v1/services/catalogue") == "w"
    assert access.bit_for("POST", "/api/v1/services/dhcp/actions/restart") == "x"
    assert access.bit_for("GET", "/api/v1/prometheus/rules", declared="w") == "w"


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
