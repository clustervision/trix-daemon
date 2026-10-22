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
Exercise every endpoint of a running trixd at every role, and replication across a pair.

Run as root on a controller, with the other controller's address as the argument:

    api-probe.py ha2-controller1.cluster:7070

What it is for: the suites cannot reach a pair of real controllers, so nothing else
exercises the role gate, the node endpoint and the crossing of a row where they actually
run. It asks the daemon what its endpoints are rather than carrying a list - every path and
verb out of /api/v1/openapi.json, with the `x-trixd-access` and `x-trixd-minimum` each one
declares - so the expectation comes off the endpoint and one added later is covered without
this file being edited.

It is read-only by construction, with one exception that is enforced rather than hoped for.
Every path variable names something that does not exist, and every non-GET body is a list
where the schema wants an object, so the role gate is what answers and no handler acts: a
403 is the gate refusing, anything else is the gate letting the call through, which is the
only question the matrix asks. The exception is the endpoints that declare `replicate`:
called with a role that is allowed, those queue an action for the peer and nothing can take
it back, so they are called only with roles that are refused. The accounts it creates are
removed again, which leaves deletion markers behind - that is the design, and removing them
would resurrect the accounts on the peer.

Three things it took to make this mean anything, all of which read as passing first:

- The document's paths carry the API prefix and so does the base URL. Left in, every call
  reached /api/v1/api/v1/... and answered 404 - which satisfies "not 403", so more than half
  the matrix passed without reaching the daemon at all. There is now a check that a known
  endpoint answers before anything is measured.
- The sweep PULLS what the peer holds; it does not push. A row written here crosses when the
  PEER's pass runs, so that is where the pass is asked for.
- The two controllers sign with their own secrets, so a token minted on one is not one the
  other can verify. The peer issues its own.
"""

import json
import subprocess
import sys
import urllib.request
import ssl

PREFIX = "/api/v1"
API = f"https://localhost:7070{PREFIX}"
INI = "/trinity/local/trixd/daemon/config/trixd.ini"
PROBE = "trixd-probe-absent"
LADDER = ["none", "reader", "operator", "manager", "rootus"]
CONTEXT = ssl._create_unverified_context()

passed, failed, notes = 0, 0, []


def ok(what):
    global passed
    passed += 1


def bad(what):
    global failed
    failed += 1
    notes.append(what)


def ini_account():
    """The account trixd.ini names, which is rootus."""
    user = password = ""
    for line in open(INI, encoding="utf-8"):
        key, _, value = line.partition("=")
        if key.strip().upper() == "USERNAME" and not user:
            user = value.strip()
        if key.strip().upper() == "PASSWORD" and not password:
            password = value.strip()
    return user, password


def call(method, path, token="", body=None, limit=400):
    """
    One request, answering (status, text). Never raises for an HTTP status.

    The body is read to `limit` so a matrix of hundreds of calls keeps nothing large; a
    caller that needs the whole answer, such as the endpoint listing, says so.
    """
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{API}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("x-access-tokens", token)
    try:
        with urllib.request.urlopen(request, timeout=30, context=CONTEXT) as answer:
            return answer.status, answer.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(limit).decode("utf-8", "replace")
    except Exception as error:                                   # a transport failure
        return 0, str(error)


def token_for(user, password):
    """A token for one account, or empty."""
    status, text = call("POST", "/auth/token", body={"username": user,
                                                     "password": password})
    if status not in (200, 201):
        return ""
    return json.loads(text).get("token", "")


def endpoints(token):
    """Every endpoint the daemon publishes, with the access it declares."""
    status, text = call("GET", "/openapi.json", token, limit=4 * 1024 * 1024)
    if status != 200:
        raise SystemExit(f"  the endpoint listing answered {status}")
    document = json.loads(text)
    found = []
    for path, verbs in document["paths"].items():
        for verb, spec in verbs.items():
            # The document's paths carry the API prefix and so does the base, so one of the
            # two has to go: left in, every call reached /api/v1/api/v1/... and answered 404
            # - which satisfies "not 403" and made the whole matrix read as mostly passing.
            found.append((verb.upper(), path.replace(PREFIX, "", 1),
                          spec.get("x-trixd-access", "?"),
                          spec.get("x-trixd-minimum", "")))
    return sorted(found, key=lambda row: (row[1], row[0]))


def harmless(method, path):
    """
    The same call aimed where it cannot do anything: a path variable that names nothing,
    and a body the endpoint's own schema refuses. The gate runs before either is looked at.
    """
    for marker in ("{name}", "{node}", "{exporter}", "{where}", "{rest}",
                   "{request_id}", "{action}"):
        path = path.replace(marker, PROBE)
    # A list where every schema here wants an object: refused after the gate, never applied.
    return path, (None if method == "GET" else [])


# Endpoints whose manifest says `replicate`, which this probe calls only with a role that
# is refused. Called with a role that is allowed, the handler runs: it finds the probe node
# in neither Luna nor on disk and does nothing locally, but it still writes the call to the
# outbox for the peer, and there is no route to take an owed row back. So "read-only by
# construction" is not true of these, and one run of an earlier version of this probe left a
# declined row riding the sweep for an hour.
REPLICATING = ("/prometheus/hw-rules", "/prometheus/hw-rules/{node}",
               "/prometheus/hw-rules/{node}/actions/rename",
               "/prometheus/hw-rules/{node}/actions/repair")


def matrix(tokens, listing):
    """
    Every endpoint at every role, against the minimum it declares. The expectation is read
    off the endpoint rather than written down beside it, so an endpoint added later is
    covered without this being edited.
    """
    for method, path, access, minimum in listing:
        if access != "role":
            continue
        if minimum not in LADDER:
            bad(f"{method} {path} declares minimum {minimum!r}, which is not a role")
            continue
        wanted = LADDER.index(minimum)
        for role, token in tokens.items():
            allowed_here = LADDER.index(role) >= wanted
            if allowed_here and path in REPLICATING and method != "GET":
                notes.append(f"{method} {path} as {role}: not called, it would queue an "
                             f"action for the peer that nothing can take back")
                continue
            where, body = harmless(method, path)
            status, text = call(method, where, token, body)
            allowed = allowed_here
            if status == 0:
                bad(f"{method} {path} as {role}: no answer ({text[:60]})")
            elif allowed and status == 403:
                bad(f"{method} {path} as {role} refused, but {minimum} is the minimum")
            elif not allowed and status != 403:
                bad(f"{method} {path} as {role} answered {status}, but {minimum} is the "
                    f"minimum: the gate let it through")
            else:
                ok(f"{method} {path} as {role}")


def unauthenticated(listing):
    """
    What a caller with no token at all may reach. A public route must answer; everything
    else must refuse, and refuse as unauthenticated rather than as forbidden.
    """
    for method, path, access, _ in listing:
        where, body = harmless(method, path)
        status, _text = call(method, where, "", body)
        if access == "public":
            if status in (401, 403):
                bad(f"{method} {path} is public and refused a caller with no token ({status})")
            else:
                ok(f"public {method} {path}")
        elif access == "forward":
            # Not gated here by design: the call goes to Luna and Luna decides. What can be
            # asserted is that it was forwarded at all, which an answer from Luna shows.
            if status == 0:
                bad(f"{method} {path} forwards nowhere: no answer")
            else:
                ok(f"{method} {path} forwards to Luna ({status})")
        elif status not in (401, 403):
            bad(f"{method} {path} needs {access} and answered {status} with no token")
        else:
            ok(f"{method} {path} refuses no token")


def node_endpoint(tokens):
    """
    The one node endpoint: no user token reaches it, not even rootus, and a node token is
    bound to its own node.
    """
    where = f"/prometheus/hw-rules/{PROBE}"
    for role, token in tokens.items():
        status, _ = call("PATCH", where, token, {})
        if status != 403:
            bad(f"the node endpoint answered {status} to a {role} token, not 403")
        else:
            ok(f"the node endpoint refuses a {role} token")


def at_peer(peer, path, token):
    """
    One read from the other controller. Its own token, because the two controllers sign
    with their own secrets - a token minted here is not one the peer can verify.
    """
    return subprocess.run(
        ["curl", "-s", "-k", "--max-time", "30", "-H", f"x-access-tokens: {token}",
         f"https://{peer}{PREFIX}{path}"],
        capture_output=True, text=True, timeout=60).stdout


def sweep_at(peer):
    """
    Make the OTHER controller run a pass. The sweep pulls what the peer holds rather than
    pushing what this one holds, so a row written here crosses when the peer's own pass
    runs - asking this side to sweep reconciles this side and proves nothing about the row.
    """
    return subprocess.run(
        ["curl", "-s", "-k", "--max-time", "60", "-X", "POST",
         "-H", f"x-access-tokens: {PEER['token']}",
         f"https://{peer}{PREFIX}/system/replication/sweep"],
        capture_output=True, text=True, timeout=90).stdout


def peer_token(peer, user, password):
    """A token the peer itself issued, for the account both controllers are configured with."""
    answer = subprocess.run(
        ["curl", "-s", "-k", "--max-time", "30", "-X", "POST",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"username": user, "password": password}),
         f"https://{peer}{PREFIX}/auth/token"],
        capture_output=True, text=True, timeout=60).stdout
    try:
        return json.loads(answer).get("token", "")
    except ValueError:
        return ""


def replication(peer):
    """
    The pair agrees, and a write on this side reaches the other. Read through each
    controller's own API, which is how an operator would look.
    """
    status, text = call("GET", "/system/replication/hashes", TOKENS["rootus"])
    if status != 200:
        bad(f"replication hashes answered {status}")
        return
    mine = json.loads(text).get("hashes") or {}
    ok("replication hashes answer")

    if not peer or not PEER.get("token"):
        notes.append("no peer token, so the crossing was not checked")
        return
    theirs = at_peer(peer, "/system/replication/hashes", PEER["token"])
    try:
        other = json.loads(theirs).get("hashes") or {}
    except ValueError:
        bad(f"the peer did not answer its hashes: {theirs[:120]}")
        return
    if not other:
        bad("the peer answered no hashes")
        return
    for table in sorted(set(mine) | set(other)):
        if mine.get(table) == other.get(table):
            ok(f"{table} agrees across the pair")
        else:
            bad(f"{table} differs across the pair: {mine.get(table)} vs {other.get(table)}")


def crossing(peer):
    """
    A principal written here reaches the other controller, and its removal follows. This is
    the one part that writes, so it writes a name nothing else uses and takes it away again.
    """
    if not peer or not PEER.get("token"):
        return
    name = "trixd-probe-crosser"
    status, _ = call("PUT", f"/identity/users/{name}", TOKENS["rootus"],
                     {"role": "reader", "password": "probe-only-password"})
    if status not in (200, 201):
        bad(f"could not write a principal to cross: {status}")
        return
    ok("a principal was written")
    sweep_at(peer)
    listed = at_peer(peer, "/identity/users", PEER["token"])
    if name in listed:
        ok("it reached the peer")
    else:
        bad(f"it did not reach the peer: {listed[:200]}")

    call("DELETE", f"/identity/users/{name}", TOKENS["rootus"])
    sweep_at(peer)
    listed = at_peer(peer, "/identity/users", PEER["token"])
    if name in listed:
        bad("the removal did not reach the peer, so the marker did not cross")
    else:
        ok("its removal reached the peer")


# The role tokens the matrix walks. The peer's own token is held apart: it is not a role,
# and left in here the matrix tried to place it on the ladder.
TOKENS = {}
PEER = {}


def main():
    peer = sys.argv[1] if len(sys.argv) > 1 else ""
    user, password = ini_account()
    root = token_for(user, password)
    if not root:
        print("  could not sign in with the account trixd.ini names; nothing was checked")
        return 2
    TOKENS["rootus"] = root
    if peer:
        PEER["token"] = peer_token(peer, user, password)
        if not PEER["token"]:
            print(f"  the peer {peer} would not issue a token, so nothing crossing was "
                  f"checked; the rest still runs")

    # Before anything of this probe's exists: comparing the tables while this side holds
    # rows the peer has not pulled yet measures the probe's own setup, not the pair.
    replication(peer)

    made = []
    for role in ("reader", "operator", "manager"):
        who = f"trixd-probe-{role}"
        secret = f"probe-{role}-password"
        status, text = call("PUT", f"/identity/users/{who}", root,
                            {"role": role, "password": secret})
        if status not in (200, 201):
            print(f"  could not create the {role} probe account: {status} {text[:120]}")
            return 2
        made.append(who)
        token = token_for(who, secret)
        if not token:
            print(f"  the {role} probe account could not sign in")
            return 2
        TOKENS[role] = token
    print(f"  tokens: {', '.join(sorted(TOKENS))}")

    try:
        listing = endpoints(root)
        print(f"  endpoints: {len(listing)}")
        # The matrix is only about the gate, so every path must at least resolve. A path
        # that 404s for everybody satisfies "not 403" and would read as a pass.
        probe_status, _ = call("GET", "/system/plugins", root)
        if probe_status != 200:
            print(f"  a known endpoint answered {probe_status}, so the paths do not "
                  f"resolve; nothing was measured")
            return 2
        matrix(TOKENS, listing)
        unauthenticated(listing)
        node_endpoint(TOKENS)
        crossing(peer)
    finally:
        for who in made:
            call("DELETE", f"/identity/users/{who}", root)
        call("POST", "/system/replication/sweep", root)

    print(f"\n  == {passed} passed, {failed} failed")
    for note in notes[:40]:
        print(f"     {note}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
