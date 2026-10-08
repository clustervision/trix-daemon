#!/bin/bash

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

# End to end against a running trixd and a real Luna. Every check prints
# PASS or FAIL and the script exits non-zero if any failed.

PLAIN=${PLAIN:-http://localhost:8051}
TLS=${TLS:-https://localhost:8050}
# The interpreter the unit actually runs, so this works wherever it is installed.
PY=${PY:-$(systemctl show trixd -p ExecStart --value 2>/dev/null |
           grep -oE '/[^ ]*/bin/python[0-9.]*' | head -1)}
PY=${PY:-$(command -v python3)}
FAIL=0

say() { printf "  %-6s %s\n" "$1" "$2"; [ "$1" = "FAIL" ] && FAIL=$((FAIL+1)); return 0; }
check() { [ "$2" = "$3" ] && say PASS "$1" || say FAIL "$1 (got $2, wanted $3)"; }
code() { curl -sk -o /dev/null -w "%{http_code}" "$@"; }
jq_() { $PY -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

echo "=== 1. the listeners ==="
check "8051 answers plain HTTP"        "$(code $PLAIN/api/v1/system/health)" 200
check "8050 answers TLS"               "$(code $TLS/api/v1/system/health)" 200
check "health needs no token"          "$(code $PLAIN/api/v1/system/health)" 200
check "the document is published"      "$(code $PLAIN/api/v1/openapi.json)" 200
check "the docs page renders"          "$(code $PLAIN/api/v1/docs)" 200

echo "=== 2. authentication ==="
check "no token is 401"                "$(code $PLAIN/api/v1/system/plugins)" 401
check "a bad token is 401"             "$(code $PLAIN/api/v1/system/plugins -H 'x-access-tokens: rubbish')" 401
BAD='{"username":"trixd","password":"no"}'
check "wrong password is 401"          "$(code -X POST $PLAIN/api/v1/auth/token -H 'Content-Type: application/json' -d "$BAD")" 401
T=$(curl -sk -X POST $PLAIN/api/v1/auth/token -H 'Content-Type: application/json' \
      -d '{"username":"trixd","password":"trixd"}' | jq_ "d['token']")
[ -n "$T" ] && say PASS "a token is minted" || say FAIL "no token minted"
H="x-access-tokens: $T"
check "the token is accepted"          "$(code $PLAIN/api/v1/system/plugins -H "$H")" 200
check "bearer works too"               "$(code $PLAIN/api/v1/system/plugins -H "Authorization: Bearer $T")" 200

echo "=== 3. the same token at Luna ==="
LUNA=$(grep -m1 "^ENDPOINT" /trinity/local/luna/daemon/config/luna.ini | sed 's/.*= *//')
check "trixd token works on Luna"       "$(code https://$LUNA/config/node -H "$H")" 200
check "the pass-through reaches Luna"  "$(code $PLAIN/api/v1/luna/config/node -H "$H")" 200
NODES=$(curl -sk $PLAIN/api/v1/luna/config/node -H "$H" | jq_ "len(d['config']['node'])")
[ "${NODES:-0}" -gt 0 ] && say PASS "the pass-through returned $NODES nodes" || say FAIL "no nodes through the pass-through"

echo "=== 4. plugins ==="
LOADED=$(curl -sk $PLAIN/api/v1/system/plugins -H "$H" | jq_ "sum(1 for p in d['plugins'] if p['state']=='loaded')")
WANTED=$(sed -n '/^\[PLUGINS\]/,/^\[/p' /trinity/local/trixd/etc/trixd.ini | grep -m1 '^ENABLED' | sed 's/.*= *//' | tr ',' '\n' | grep -c .)
check "every enabled plugin loaded"    "$LOADED" "$WANTED"
check "an unknown plugin is 404"       "$(code -X POST $PLAIN/api/v1/system/plugins/nosuch/reload -H "$H")" 404
check "auth is reserved"               "$(code -X POST $PLAIN/api/v1/system/plugins/auth/reload -H "$H")" 409

echo "=== 5. hot unplug and plug ==="
PID1=$(systemctl show trixd -p MainPID --value)
curl -sk -o /dev/null -X POST $PLAIN/api/v1/system/plugins/services/disable -H "$H"
check "routes gone after unplug"       "$(code $PLAIN/api/v1/services/catalogue -H "$H")" 404
curl -sk -o /dev/null -X POST $PLAIN/api/v1/system/plugins/services/reload -H "$H"
check "routes back after re-plug"      "$(code $PLAIN/api/v1/services/catalogue -H "$H")" 200
PID2=$(systemctl show trixd -p MainPID --value)
check "the daemon never restarted"     "$PID1" "$PID2"

echo "=== 6. renders ==="
check "targets render answers"         "$(code $PLAIN/api/v1/prometheus/renders/targets)" 200
ONE=$(curl -sk $PLAIN/api/v1/prometheus/renders/targets | jq_ "len(d)")
[ "${ONE:-0}" -gt 0 ] && say PASS "service discovery has $ONE entries" || say FAIL "service discovery is empty"
TWO=$(curl -sk $PLAIN/api/v1/prometheus/renders/targets | jq_ "len(d)")
check "the render is stable"           "$ONE" "$TWO"

echo "=== 7. the queue, and the status of a queued action ==="
R=$(curl -sk -X POST $PLAIN/api/v1/services/slurmctld/actions/restart -H "$H")
ID=$(echo "$R" | jq_ "d['request_id']")
[ -n "$ID" ] && say PASS "an action is queued as $ID" || say FAIL "no request id"
# systemctl on a Type=notify unit takes seconds, which is the whole reason this
# is queued. Poll for it rather than guessing how long it needs.
STATE=running
for _ in $(seq 1 30); do
  STATE=$(curl -sk $PLAIN/api/v1/services/status/$ID -H "$H" | jq_ "d['state']")
  [ "$STATE" = "done" ] || [ "$STATE" = "failed" ] && break
  sleep 1
done
check "the queued action finished"     "$STATE" done
check "an unknown request is 404"      "$(code $PLAIN/api/v1/services/status/nope -H "$H")" 404

echo "=== 8. identity and RBAC ==="
J='Content-Type: application/json'
curl -sk -o /dev/null -X PUT $PLAIN/api/v1/identity/users/e2enone -H "$H" -H "$J" -d '{"password":"pw"}'
curl -sk -o /dev/null -X PUT $PLAIN/api/v1/identity/users/e2eop -H "$H" -H "$J" -d '{"password":"pw","role":"operator"}'
NT0=$(curl -sk -X POST $PLAIN/api/v1/auth/token -H "$J" -d '{"username":"e2enone","password":"pw"}' | jq_ "d['token']")
OT=$(curl -sk -X POST $PLAIN/api/v1/auth/token -H "$J" -d '{"username":"e2eop","password":"pw"}' | jq_ "d['token']")
NH="x-access-tokens: $NT0"; OH="x-access-tokens: $OT"
[ -n "$NT0" ] && [ -n "$OT" ] && say PASS "both principals can log in" || say FAIL "a principal cannot log in"
check "no role may look"               "$(code $PLAIN/api/v1/prometheus/config -H "$NH")" 200
check "no role may not change"         "$(code -X PATCH $PLAIN/api/v1/prometheus/hw-rules -H "$NH" -H "$J" -d '{"nodes":[]}')" 403
check "no role may not operate"        "$(code -X POST $PLAIN/api/v1/services/slurmctld/actions/status -H "$NH")" 403
check "an operator may operate"        "$(code -X POST $PLAIN/api/v1/services/slurmctld/actions/status -H "$OH")" 200
check "an operator may not change"     "$(code -X PATCH $PLAIN/api/v1/prometheus/hw-rules -H "$OH" -H "$J" -d '{"nodes":[]}')" 403
check "rootus is never refused"        "$(code -X PATCH $PLAIN/api/v1/prometheus/hw-rules -H "$H" -H "$J" -d '{"nodes":[]}')" 200
check "a bad role is refused"          "$(code -X PUT $PLAIN/api/v1/identity/users/e2eking -H "$H" -H "$J" -d '{"role":"king"}')" 400
curl -sk -o /dev/null -X PUT $PLAIN/api/v1/identity/users/e2eop -H "$H" -H "$J" -d '{"role":"manager"}'
check "promoted, the same token changes" "$(code -X PATCH $PLAIN/api/v1/prometheus/hw-rules -H "$OH" -H "$J" -d '{"nodes":[]}')" 200
check "a personal token is nobody at Luna" "$(code https://$LUNA/config/osuser -H "$NH")" 401
check "nor through the pass-through"   "$(code $PLAIN/api/v1/luna/config/node -H "$OH")" 401

echo "=== 9. whoami ==="
WHO=$(curl -sk $PLAIN/api/v1/auth/whoami -H "$OH")
check "whoami names the principal"     "$(echo "$WHO" | jq_ "d['principal']")" e2eop
check "whoami reports the role"        "$(echo "$WHO" | jq_ "d['role']")" manager
check "whoami says not rootus"         "$(echo "$WHO" | jq_ "d['rootus']")" False

echo "=== 10. the audit trail ==="
GRANTS=$($PY -c "
import sqlite3
c=sqlite3.connect('/trinity/local/trixd/var/trixd.db')
print(c.execute(\"select count(*) from audit where outcome='granted'\").fetchone()[0])")
REFUSALS=$($PY -c "
import sqlite3
c=sqlite3.connect('/trinity/local/trixd/var/trixd.db')
print(c.execute(\"select count(*) from audit where outcome='refused'\").fetchone()[0])")
[ "${GRANTS:-0}" -gt 0 ] && say PASS "$GRANTS grants recorded" || say FAIL "no grants recorded"
[ "${REFUSALS:-0}" -gt 0 ] && say PASS "$REFUSALS refusals recorded" || say FAIL "no refusals recorded"

echo "=== 11. node tokens ==="
check "anonymous node token refused"   "$(code -X POST $PLAIN/api/v1/auth/node/node001 -H 'Content-Type: application/json' -d '{}')" 401
CRED='{"username":"trixd","password":"trixd"}'
check "node token on a credential"     "$(code -X POST $PLAIN/api/v1/auth/node/node001 -H 'Content-Type: application/json' -d "$CRED")" 201
NT=$(curl -sk -X POST $PLAIN/api/v1/auth/node/node001 -H 'Content-Type: application/json' -d '{"username":"trixd","password":"trixd"}' | jq_ "d['token']")
check "a node token is not a user one" "$(code $PLAIN/api/v1/identity/users -H "x-access-tokens: $NT")" 403

echo "=== 12. replication endpoints ==="
check "table hashes answer"            "$(code $PLAIN/api/v1/system/replication/hashes -H "$H")" 200
check "one table exports"              "$(code $PLAIN/api/v1/system/replication/table/principal -H "$H")" 200
HEALTH=$(curl -sk $PLAIN/api/v1/system/health)
check "health reports the renders"     "$(echo "$HEALTH" | jq_ "len(d['renders']) > 0")" True
check "health reports the triggers"    "$(echo "$HEALTH" | jq_ "'luna_hashes' in d['triggers']")" True

echo "=== 13. tidy up ==="
curl -sk -o /dev/null -X DELETE $PLAIN/api/v1/identity/users/e2eop -H "$H"
curl -sk -o /dev/null -X DELETE $PLAIN/api/v1/identity/users/e2enone -H "$H"
say PASS "removed the test principals"

echo
echo "failures: $FAIL"
exit $FAIL
