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
Who may call what, over HTTP, for every published route: each identity on the ladder,
a node token, and every kind of bad token. Generated from docs/openapi.json, so a new
route or plugin is covered the moment it is published.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import base64
import json

import pytest

from tests.e2e.stack import MARK, RANK, fill

IDENTITIES = ("anonymous", "none", "reader", "operator", "manager", "rootus")
BODY_VERBS = ("POST", "PATCH", "PUT")


def _token(stack, who: str) -> str:
    """
    The token an identity carries, or none at all.
    """
    return None if who == "anonymous" else stack.as_role(who)


def _send(stack, verb: str, path: str, token: str = None, node: str = None, headers=None):
    """
    The route, filled in with a harmless name, with an empty body where it takes one.
    """
    body = {} if verb in BODY_VERBS else None
    return stack.call(verb, fill(path, node or f"{MARK}x"), token, json=body, headers=headers)


@pytest.mark.parametrize("who", IDENTITIES)
def test_a_role_route_admits_exactly_the_roles_at_or_above_its_minimum(stack, operation, who):
    """
    Below the minimum is a 403 naming it; at or above it is never 401, 403 or 500.
    """
    verb, path, described = operation
    minimum = described["x-trixd-minimum"]
    answer = _send(stack, verb, path, _token(stack, who))
    if who == "anonymous":
        assert answer.status_code == 401, answer.text
    elif RANK[who] >= RANK[minimum]:
        assert answer.status_code not in (401, 403, 500), (who, answer.status_code, answer.text)
    else:
        assert answer.status_code == 403, (who, answer.status_code, answer.text)
        assert minimum in answer.text


test_a_role_route_admits_exactly_the_roles_at_or_above_its_minimum.access = ("role",)


def test_a_node_token_is_refused_on_every_role_route(stack, operation):
    """
    A node's token opens its own node route and nothing a person uses.
    """
    if not stack.nodes:
        pytest.skip("Luna holds no node")
    verb, path, _ = operation
    answer = _send(stack, verb, path, stack.node_token(stack.nodes[0]))
    assert answer.status_code == 403, answer.text


test_a_node_token_is_refused_on_every_role_route.access = ("role",)


@pytest.mark.parametrize("who", IDENTITIES)
def test_a_public_route_answers_anyone(stack, operation, who):
    """
    Health, version, signing in and service discovery need no role. The sign-in routes may
    still refuse a wrong credential, which is theirs to do, not the gate's.
    """
    verb, path, _ = operation
    answer = _send(stack, verb, path, _token(stack, who))
    assert answer.status_code != 500, answer.text
    if not path.startswith("/auth/"):
        assert answer.status_code not in (401, 403), answer.text
        return

    # A sign-in route refuses a wrong credential with a 401 of its own, and the gate refuses
    # a missing token with a 401 as well - so the status alone cannot tell a route that is
    # public from one that has quietly been given a guard. What separates them is that a
    # public route does not look at the token: it must answer the same to everyone, and the
    # body it sends back must be the same answer, not a different refusal.
    assert answer.status_code in (401, 422), answer.text
    anonymous = _send(stack, verb, path, _token(stack, "anonymous"))
    assert answer.status_code == anonymous.status_code, (
        f"{verb} {path} answered {who} {answer.status_code} and anonymous "
        f"{anonymous.status_code}: a public route does not read the token, so one of these "
        f"is the gate refusing")
    assert answer.text == anonymous.text, (
        f"{verb} {path} gave {who} a different answer from anonymous, so something is "
        f"reading the caller: {answer.text[:120]} against {anonymous.text[:120]}")


test_a_public_route_answers_anyone.access = ("public",)


@pytest.mark.parametrize("who", IDENTITIES)
def test_a_node_route_takes_only_that_nodes_token(stack, operation, who):
    """
    No person's token opens a node route, rootus included.
    """
    verb, path, _ = operation
    node = stack.nodes[0] if stack.nodes else f"{MARK}x"
    answer = _send(stack, verb, path, _token(stack, who), node=node)
    assert answer.status_code == (401 if who == "anonymous" else 403), answer.text


test_a_node_route_takes_only_that_nodes_token.access = ("node",)


def test_a_node_token_opens_its_own_node_route_and_no_other(stack, operation):
    """
    Its own node: not refused. Another node: 403.
    """
    if len(stack.nodes) < 2:
        pytest.skip("needs two nodes at Luna")
    verb, path, _ = operation
    own, other = stack.nodes[0], stack.nodes[1]
    token = stack.node_token(own)
    assert _send(stack, verb, path, token, node=own).status_code not in (401, 403, 500)
    assert _send(stack, verb, path, token, node=other).status_code == 403


test_a_node_token_opens_its_own_node_route_and_no_other.access = ("node",)


def _unsigned(subject: str) -> str:
    """
    A token with alg none: no signature at all.
    """
    def part(data: dict) -> str:
        """
        One base64url segment.
        """
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part({'sub': subject, 'id': 0})}."


BAD_TOKENS = ("garbage", "unsigned", "foreign-secret", "expired", "bearer-empty", "basic")


@pytest.mark.parametrize("kind", BAD_TOKENS)
def test_no_bad_token_passes_any_role_route(stack, operation, kind):
    """
    Garbage, an unsigned token, one signed elsewhere, an expired one, an empty Bearer and
    Basic auth are all 401, on every role route.
    """
    verb, path, _ = operation
    headers, token = {}, None
    if kind == "garbage":
        token = "not.a.token"
    elif kind == "unsigned":
        token = _unsigned(stack.user)
    elif kind == "foreign-secret":
        token = stack.mint(stack.user, secret="f" * 32, id=0)
    elif kind == "expired":
        if not stack.secret:
            pytest.skip("needs the shared secret: set TRIXD_E2E_SECRET")
        token = stack.mint(stack.user, expiry=-60, id=0)
    elif kind == "bearer-empty":
        headers = {"Authorization": "Bearer "}
    else:
        headers = {"Authorization": "Basic dHJpeGQ6dHJpeGQ="}
    answer = _send(stack, verb, path, token, headers=headers)
    assert answer.status_code == 401, (kind, answer.status_code, answer.text)


test_no_bad_token_passes_any_role_route.access = ("role",)


def test_luna_decides_on_the_route_to_luna(stack):
    """
    rootus reaches Luna as its administrator; anyone else is nobody there; no token is 401.
    """
    assert stack.call("GET", "/luna/config/node", stack.rootus()).status_code == 200
    assert stack.call("GET", "/luna/config/node", stack.as_role("manager")).status_code == 401
    assert stack.call("GET", "/luna/config/node").status_code == 401
