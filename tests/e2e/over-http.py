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

"""End to end over HTTP alone, so it runs from a workstation with no shell access."""
import json
import sys
import urllib.request
import urllib.error
import ssl
import time

if len(sys.argv) < 2:
    sys.exit("usage: over-http.py <controller>")
HOST = sys.argv[1]
PLAIN = f"http://{HOST}:8051/api/v1"
TLS = f"https://{HOST}:8050/api/v1"
ADMIN = ("trix", "trix")

RELAXED = ssl.create_default_context()
RELAXED.check_hostname = False
RELAXED.verify_mode = ssl.CERT_NONE

failures = []


def call(method, url, token="", body=None):
    """One request, returning the status and the decoded body."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("x-access-tokens", token)
    try:
        with urllib.request.urlopen(request, timeout=30, context=RELAXED) as answer:
            raw = answer.read().decode()
            return answer.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"body": raw[:120]}
    except Exception as error:
        return 0, {"error": str(error)[:120]}


def check(label, got, want):
    """Record one expectation."""
    if got == want:
        print(f"  PASS   {label}")
    else:
        print(f"  FAIL   {label} (got {got!r}, wanted {want!r})")
        failures.append(label)


print("=== listeners and document ===")
check("plain HTTP health", call("GET", f"{PLAIN}/system/health")[0], 200)
check("TLS health", call("GET", f"{TLS}/system/health")[0], 200)
status, health = call("GET", f"{PLAIN}/system/health")
listed = call("GET", f"{PLAIN}/system/plugins", call("POST", f"{PLAIN}/auth/token",
              body={"username": ADMIN[0], "password": ADMIN[1]})[1].get("token", ""))[1]
refused = [p["name"] for p in listed.get("plugins", []) if p["state"] == "refused" and p["name"] != "luna"]
check("no plugin refused", refused, [])
check("the document is published", call("GET", f"{PLAIN}/openapi.json")[0], 200)
status, spec = call("GET", f"{PLAIN}/openapi.json")
print(f"         {len(spec.get('paths', {}))} paths published")

print("=== authentication ===")
check("no token refused", call("GET", f"{PLAIN}/system/plugins")[0], 401)
check("bad token refused", call("GET", f"{PLAIN}/system/plugins", "rubbish")[0], 401)
check("wrong password refused",
      call("POST", f"{PLAIN}/auth/token",
           body={"username": ADMIN[0], "password": "no"})[0], 401)
status, minted = call("POST", f"{PLAIN}/auth/token",
                      body={"username": ADMIN[0], "password": ADMIN[1]})
check("a token is minted", status, 201)
T = minted.get("token", "")
check("the token is accepted", call("GET", f"{PLAIN}/system/plugins", T)[0], 200)

print("=== the pass-through to Luna ===")
status, nodes = call("GET", f"{PLAIN}/luna/config/node", T)
check("Luna answers through us", status, 200)
count = len((nodes.get("config") or {}).get("node") or {})
print(f"         {count} nodes returned")
check("Luna returned nodes", count > 0, True)

print("=== hot unplug and plug ===")
check("unplug", call("POST", f"{PLAIN}/system/plugins/services/disable", T)[0], 200)
check("routes gone", call("GET", f"{PLAIN}/services/catalogue", T)[0], 404)
check("re-plug", call("POST", f"{PLAIN}/system/plugins/services/reload", T)[0], 200)
check("routes back", call("GET", f"{PLAIN}/services/catalogue", T)[0], 200)

print("=== renders ===")
status, first = call("GET", f"{PLAIN}/prometheus/renders/targets")
check("service discovery answers", status, 200)
print(f"         {len(first)} targets")
status, again = call("GET", f"{PLAIN}/prometheus/renders/targets")
check("the render is stable", first == again, True)

print("=== the queue and the status of a queued action ===")
status, queued = call("POST", f"{PLAIN}/services/slurmctld/actions/restart", T)
check("an action is queued", status, 200)
identifier = queued.get("request_id", "")
state = "running"
for _ in range(30):
    status, polled = call("GET", f"{PLAIN}/services/status/{identifier}", T)
    state = polled.get("state")
    if state in ("done", "failed"):
        break
    time.sleep(1)
check("the queued action finished", state, "done")
check("unknown request is 404",
      call("GET", f"{PLAIN}/services/status/nope", T)[0], 404)

print("=== identity and RBAC ===")
call("PUT", f"{PLAIN}/identity/users/httpnone", T, {"password": "pw"})
check("a user is created with a role",
      call("PUT", f"{PLAIN}/identity/users/httpop", T,
           {"password": "pw", "role": "operator"})[0], 200)
check("a bad role is refused",
      call("PUT", f"{PLAIN}/identity/users/httpking", T, {"role": "king"})[0], 400)
N = call("POST", f"{PLAIN}/auth/token",
         body={"username": "httpnone", "password": "pw"})[1].get("token", "")
status, theirs = call("POST", f"{PLAIN}/auth/token",
                      body={"username": "httpop", "password": "pw"})
R = theirs.get("token", "")
check("the operator can log in", status, 201)
check("no role may look", call("GET", f"{PLAIN}/prometheus/config", N)[0], 200)
check("no role may not change", call("PATCH", f"{PLAIN}/prometheus/hw-rules", N, {"nodes": []})[0], 403)
check("no role may not operate",
      call("POST", f"{PLAIN}/services/slurmctld/actions/status", N)[0], 403)
check("an operator may operate",
      call("POST", f"{PLAIN}/services/slurmctld/actions/status", R)[0], 200)
check("an operator may not change", call("PATCH", f"{PLAIN}/prometheus/hw-rules", R, {"nodes": []})[0], 403)
status, said = call("PATCH", f"{PLAIN}/prometheus/hw-rules", T, {"nodes": [{"node": "no-such-node"}]})
check("an unknown node is named, not written", (status, said.get("nodes", [{}])[0].get("ok")), (200, False))
check("rootus is never refused", call("PATCH", f"{PLAIN}/prometheus/hw-rules", T, {"nodes": []})[0], 200)
call("PUT", f"{PLAIN}/identity/users/httpop", T, {"role": "manager"})
check("promoted, the same token changes", call("PATCH", f"{PLAIN}/prometheus/hw-rules", R, {"nodes": []})[0], 200)
check("a personal token is nobody at Luna", call("GET", f"{PLAIN}/luna/config/node", R)[0], 401)

print("=== whoami ===")
status, said = call("GET", f"{PLAIN}/auth/whoami", R)
check("whoami names the principal", said.get("principal"), "httpop")
check("whoami reports the role", said.get("role"), "manager")
check("whoami says not rootus", said.get("rootus"), False)

print("=== disabling cuts at once ===")
call("PUT", f"{PLAIN}/identity/users/httpop", T, {"enabled": False})
check("the same token stops working", call("GET", f"{PLAIN}/auth/whoami", R)[0], 401)
call("PUT", f"{PLAIN}/identity/users/httpop", T, {"enabled": True})
check("and works again when re-enabled", call("GET", f"{PLAIN}/auth/whoami", R)[0], 200)

print("=== node tokens ===")
check("anonymous node token refused",
      call("POST", f"{PLAIN}/auth/node/node001", body={})[0], 401)
check("node token on a credential",
      call("POST", f"{PLAIN}/auth/node/node001",
           body={"username": ADMIN[0], "password": ADMIN[1]})[0], 201)

print("=== replication endpoints ===")
for name in ("principal", "declaration"):
    status, table = call("GET", f"{PLAIN}/system/replication/table/{name}", T)
    documents = table.get("documents") or []
    carried = sorted(documents[0]) if documents else []
    print(f"  {'PASS  ' if status == 200 else 'FAIL  '} {name} exports {len(documents)}")
    if status != 200:
        failures.append(name)
    if name == "principal" and documents:
        check("principal carries the role", "role" in carried, True)

print("=== tidy up ===")
call("DELETE", f"{PLAIN}/identity/users/httpop", T)
call("DELETE", f"{PLAIN}/identity/users/httpnone", T)
print("  PASS   removed the test principals")

print(f"\nfailures: {len(failures)}")
for item in failures:
    print(f"  {item}")
sys.exit(1 if failures else 0)
