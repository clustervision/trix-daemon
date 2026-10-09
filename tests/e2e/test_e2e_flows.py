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
Regression flows, each a short story a user or a node lives through: a user's role over
its life, the audit trail, a queued service action, a node's hardware rules after boot,
and the route to Luna.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import time

import pytest

from tests.e2e.stack import MARK


def test_health_version_and_the_published_document(stack):
    """
    The three things a client reads before anything else.
    """
    health = stack.call("GET", "/system/health")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    version = stack.call("GET", "/system/version").json()["version"]
    assert version["trixd"].startswith("1.") and version["api"] == 1
    document = stack.document()
    assert document["info"]["title"] == "TrinityX Daemon (trixd)"
    assert any(path.startswith("/api/v1/luna/") for path in document["paths"])
    page = stack.call("GET", "/docs")
    assert page.status_code == 200 and "https://cdn" not in page.text
    bundle = stack.call("GET", "/docs/static/vendor/swagger-ui/swagger-ui-bundle.js")
    assert bundle.status_code == 200
    assert "GNU GENERAL PUBLIC LICENSE" in stack.call("GET", "/system/license").text


@pytest.mark.parametrize("role", ["none", "reader", "operator", "manager", "rootus"])
def test_whoami_says_what_each_role_holds(stack, role):
    """
    The ladder as a caller sees it.
    """
    answer = stack.call("GET", "/auth/whoami", stack.as_role(role)).json()
    assert answer["role"] == ("none" if role == "rootus" else role) or answer.get("rootus")


@pytest.mark.parametrize("how", ["x-access-tokens", "bearer"])
def test_a_token_works_in_either_header(stack, how):
    """
    x-access-tokens, as Luna takes it, or Authorization: Bearer.
    """
    token = stack.as_role("reader")
    headers = ({"Authorization": f"Bearer {token}"} if how == "bearer"
               else {"x-access-tokens": token})
    assert stack.call("GET", "/system/plugins", headers=headers).status_code == 200


def test_a_wrong_password_and_an_unknown_user_are_both_401(stack):
    """
    Signing in tells nothing about which part was wrong.
    """
    assert stack.sign_in(stack.user, "wrong").status_code == 401
    assert stack.sign_in(f"{MARK}nobody", "wrong").status_code == 401


def test_a_users_role_changes_apply_to_the_token_already_held(stack):
    """
    Created as reader, promoted, demoted, disabled, deleted: each step counts at once.
    """
    name = f"{MARK}life"
    root = stack.rootus()
    assert stack.call("PUT", f"/identity/users/{name}", root,
                      json={"password": "pw-life", "role": "reader"}).status_code == 200
    token = stack.sign_in(name, "pw-life").json()["token"]
    change = ("PATCH", "/services/catalogue")
    assert stack.call("GET", "/services/catalogue", token).status_code == 200
    assert stack.call(*change, token, json={}).status_code == 403

    stack.call("PUT", f"/identity/users/{name}", root, json={"role": "manager"})
    assert stack.call(*change, token, json={}).status_code not in (401, 403)
    stack.call("PUT", f"/identity/users/{name}", root, json={"role": "none"})
    assert stack.call("GET", "/services/catalogue", token).status_code == 403

    stack.call("PUT", f"/identity/users/{name}", root, json={"enabled": False})
    assert stack.call("GET", "/auth/whoami", token).status_code == 401
    assert stack.sign_in(name, "pw-life").status_code == 401

    assert stack.call("DELETE", f"/identity/users/{name}", root).status_code == 200
    assert stack.call("GET", "/auth/whoami", token).status_code == 401
    stack.call("PUT", f"/identity/users/{name}", root, json={"password": "pw-life"})
    again = stack.sign_in(name, "pw-life").json()["token"]
    assert stack.call("GET", "/auth/whoami", again).json()["role"] == "none"
    stack.call("DELETE", f"/identity/users/{name}", root)


def test_rootus_cannot_be_given_and_a_role_off_the_ladder_is_refused(stack):
    """
    rootus is the ini account alone; a typo is a 400.
    """
    root = stack.rootus()
    for role in ("rootus", "admin", "Manager"):
        answer = stack.call("PUT", f"/identity/users/{MARK}bad", root, json={"role": role})
        assert answer.status_code == 400, (role, answer.text)


def test_a_refusal_lands_in_the_audit_trail_a_reader_can_read(stack):
    """
    The read-only user sees who was refused what.
    """
    stack.call("GET", "/services/catalogue", stack.as_role("none"))
    time.sleep(1)
    rows = stack.call("GET", "/system/audit", stack.as_role("reader"),
                      params={"principal": f"{MARK}none", "outcome": "refused"}).json()["audit"]
    assert rows and rows[0]["object"].endswith("/services/catalogue")


def test_a_service_action_is_queued_and_its_state_polled(stack):
    """
    An operator asks, gets a request id at once, and the state moves on.
    """
    catalogue = stack.call("GET", "/services/catalogue", stack.as_role("reader")).json()
    names = sorted((catalogue.get("services") or {}))
    assert names, "an empty catalogue"
    name = "prometheus-server" if "prometheus-server" in names else names[0]
    answer = stack.call("POST", f"/services/{name}/actions/reload", stack.as_role("operator"))
    assert answer.status_code == 200, answer.text
    request_id = answer.json()["request_id"]
    state = ""
    for _ in range(40):
        state = stack.call("GET", f"/services/status/{request_id}",
                           stack.as_role("reader")).json()["state"]
        if state in ("done", "failed"):
            break
        time.sleep(0.25)
    assert state in ("done", "failed"), state


def test_a_node_after_boot_gets_its_rules_once_and_then_costs_nothing(stack):
    """
    The node hook's path: a node token, then its own hardware rules; asked again with
    nothing changed, the answer says so.
    """
    if not stack.nodes:
        pytest.skip("Luna holds no node")
    node = stack.nodes[0]
    token = stack.node_token(node)
    first = stack.call("PATCH", f"/prometheus/hw-rules/{node}", token, json={"force": False})
    if first.status_code == 502 and not stack.local:
        pytest.skip(f"Prometheus has no hardware data for {node} yet: {first.text}")
    assert first.status_code == 200, first.text
    again = stack.call("PATCH", f"/prometheus/hw-rules/{node}", token, json={"force": False})
    assert again.status_code == 200 and again.json()["changed"] is False


def test_the_hardware_rules_of_every_node_are_listed_for_a_reader(stack):
    """
    Luna's export, as a route: all, some by name, counts only, a missing one named.
    """
    every = stack.call("GET", "/prometheus/hw-rules", stack.as_role("reader"))
    assert every.status_code == 200
    held = [item["node"] for item in every.json()["nodes"]]
    some = stack.call("GET", "/prometheus/hw-rules", stack.as_role("reader"),
                      params={"nodes": f"{MARK}missing", "detail": "no"}).json()["nodes"]
    assert some == [{"node": f"{MARK}missing", "exists": False}]
    if held:
        one = stack.call("GET", "/prometheus/hw-rules", stack.as_role("reader"),
                         params={"nodes": held[0], "detail": "no"}).json()["nodes"][0]
        assert one["exists"] is True and "document" not in one


def test_a_manager_switches_a_nodes_alerts_and_the_old_file_is_kept(stack):
    """
    nhc off, then on again: each change writes and is backed up; the same again is no change.
    """
    if not stack.local:
        pytest.skip("reads the rules directory on the daemon's disk")
    node = stack.nodes[0]
    manager = stack.as_role("manager")
    stack.call("PATCH", "/prometheus/hw-rules", manager,
               json={"nodes": [{"node": node, "force": True}]})
    off = stack.call("PATCH", "/prometheus/hw-rules", manager,
                     json={"nodes": [{"node": node, "nhc": False}]}).json()["nodes"][0]
    same = stack.call("PATCH", "/prometheus/hw-rules", manager,
                      json={"nodes": [{"node": node, "nhc": False}]}).json()["nodes"][0]
    assert off["changed"] is True and same["changed"] is False
    backups = os.listdir(os.path.join(stack.scratch, "rules", "backup"))
    assert any(name.startswith(f"trix.hw.{node}") for name in backups)


def test_a_node_luna_does_not_know_is_reported_by_name(stack):
    """
    Not written, not an error for the others.
    """
    answer = stack.call("PATCH", "/prometheus/hw-rules", stack.as_role("manager"),
                        json={"nodes": [{"node": f"{MARK}ghost", "force": True}]})
    assert answer.status_code == 200
    assert answer.json()["nodes"][0] == {"node": f"{MARK}ghost", "ok": False,
                                         "message": "not a node Luna knows"}


def test_a_node_renamed_through_trixd_keeps_its_rules(stack):
    """
    The rename goes to Luna through the luna plugin, then the hook moves the rule file.
    Local only: it renames a node.
    """
    if not stack.local:
        pytest.skip("renames a node at Luna; tests/e2e/node-rename.sh does it on a controller")
    node, new = stack.nodes[-1], f"{MARK}renamed"
    manager = stack.as_role("manager")
    stack.call("PATCH", "/prometheus/hw-rules", manager, json={"nodes": [{"node": node, "force": True}]})
    rename = {"config": {"node": {node: {"newnodename": new}}}}
    assert stack.call("POST", f"/luna/config/node/{node}", stack.rootus(),
                      json=rename).status_code == 204
    found = []
    for _ in range(40):
        found = os.listdir(os.path.join(stack.scratch, "rules"))
        if f"trix.hw.{new}.cluster.rules" in found:
            break
        time.sleep(0.25)
    assert f"trix.hw.{new}.cluster.rules" in found, found
    back = {"config": {"node": {new: {"newnodename": node}}}}
    assert stack.call("POST", f"/luna/config/node/{new}", stack.rootus(), json=back).status_code == 204
