#!/bin/bash
# End to end against a running trix-daemon and a real Luna. Every check prints
# PASS or FAIL and the script exits non-zero if any failed.

PLAIN=${PLAIN:-http://localhost:8051}
TLS=${TLS:-https://localhost:8050}
PY=${PY:-/root/trixtest/venv/bin/python}
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
BAD='{"username":"trix","password":"no"}'
check "wrong password is 401"          "$(code -X POST $PLAIN/api/v1/auth/token -H 'Content-Type: application/json' -d "$BAD")" 401
T=$(curl -sk -X POST $PLAIN/api/v1/auth/token -H 'Content-Type: application/json' \
      -d '{"username":"trix","password":"trix"}' | jq_ "d['token']")
[ -n "$T" ] && say PASS "a token is minted" || say FAIL "no token minted"
H="x-access-tokens: $T"
check "the token is accepted"          "$(code $PLAIN/api/v1/system/plugins -H "$H")" 200
check "bearer works too"               "$(code $PLAIN/api/v1/system/plugins -H "Authorization: Bearer $T")" 200

echo "=== 3. the same token at Luna ==="
LUNA=$(grep -m1 "^ENDPOINT" /trinity/local/trix/etc/trix.ini | sed 's/.*= *//')
check "trix token works on Luna"       "$(code https://$LUNA/config/node -H "$H")" 200
check "the pass-through reaches Luna"  "$(code $PLAIN/api/v1/luna/config/node -H "$H")" 200
NODES=$(curl -sk $PLAIN/api/v1/luna/config/node -H "$H" | jq_ "len(d['config']['node'])")
[ "${NODES:-0}" -gt 0 ] && say PASS "the pass-through returned $NODES nodes" || say FAIL "no nodes through the pass-through"

echo "=== 4. plugins ==="
LOADED=$(curl -sk $PLAIN/api/v1/system/plugins -H "$H" | jq_ "sum(1 for p in d['plugins'] if p['state']=='loaded')")
check "five plugins loaded"            "$LOADED" 5
check "an unknown plugin is 404"       "$(code -X POST $PLAIN/api/v1/system/plugins/nosuch/reload -H "$H")" 404
check "luna is reserved"               "$(code -X POST $PLAIN/api/v1/system/plugins/luna/reload -H "$H")" 409

echo "=== 5. hot unplug and plug ==="
PID1=$(systemctl show trix-daemon -p MainPID --value)
curl -sk -o /dev/null -X POST $PLAIN/api/v1/system/plugins/genders/disable -H "$H"
check "routes gone after unplug"       "$(code $PLAIN/api/v1/genders/config -H "$H")" 404
curl -sk -o /dev/null -X POST $PLAIN/api/v1/system/plugins/genders/reload -H "$H"
check "routes back after re-plug"      "$(code $PLAIN/api/v1/genders/config -H "$H")" 200
PID2=$(systemctl show trix-daemon -p MainPID --value)
check "the daemon never restarted"     "$PID1" "$PID2"

echo "=== 6. renders ==="
check "targets render answers"         "$(code $PLAIN/api/v1/prometheus/renders/targets)" 200
ONE=$(curl -sk $PLAIN/api/v1/prometheus/renders/targets | jq_ "len(d)")
[ "${ONE:-0}" -gt 0 ] && say PASS "service discovery has $ONE entries" || say FAIL "service discovery is empty"
TWO=$(curl -sk $PLAIN/api/v1/prometheus/renders/targets | jq_ "len(d)")
check "the render is stable"           "$ONE" "$TWO"

echo "=== 7. the queue, and the thirteenth route ==="
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
curl -sk -o /dev/null -X POST $PLAIN/api/v1/identity/usergroups -H "$H" -H 'Content-Type: application/json' -d '{"name":"e2e"}'
curl -sk -o /dev/null -X POST $PLAIN/api/v1/identity/principals  -H "$H" -H 'Content-Type: application/json' -d '{"name":"e2ereader","password":"pw"}'
curl -sk -o /dev/null -X POST $PLAIN/api/v1/identity/usergroups/e2e/members -H "$H" -H 'Content-Type: application/json' -d '{"principal":"e2ereader","role":"reader"}'
curl -sk -o /dev/null -X PATCH $PLAIN/api/v1/system/acl/genders -H "$H" -H 'Content-Type: application/json' -d '{"usergroups":"e2e"}'
RT=$(curl -sk -X POST $PLAIN/api/v1/auth/token -H 'Content-Type: application/json' -d '{"username":"e2ereader","password":"pw"}' | jq_ "d['token']")
RH="x-access-tokens: $RT"
[ -n "$RT" ] && say PASS "the reader can log in" || say FAIL "the reader cannot log in"
check "a reader may look"              "$(code $PLAIN/api/v1/genders/config -H "$RH")" 200
check "a reader may not change"        "$(code -X PATCH $PLAIN/api/v1/genders/config -H "$RH" -H 'Content-Type: application/json' -d '{}')" 403
check "an outsider sees nothing"       "$(code $PLAIN/api/v1/slurm/config -H "$RH")" 403
check "rootus is never refused"        "$(code -X PATCH $PLAIN/api/v1/genders/config -H "$H" -H 'Content-Type: application/json' -d '{}')" 200
KING='{"principal":"e2ereader","role":"king"}'
check "a bad role is refused"          "$(code -X POST $PLAIN/api/v1/identity/usergroups/e2e/members -H "$H" -H 'Content-Type: application/json' -d "$KING")" 400

echo "=== 9. whoami ==="
WHO=$(curl -sk $PLAIN/api/v1/auth/whoami -H "$RH")
check "whoami names the reader"        "$(echo "$WHO" | jq_ "d['principal']")" e2ereader
check "whoami reports the role"        "$(echo "$WHO" | jq_ "d['usergroups']['e2e']")" reader
check "whoami says not admin"          "$(echo "$WHO" | jq_ "d['admin']")" False

echo "=== 10. the audit trail ==="
GRANTS=$($PY -c "
import sqlite3
c=sqlite3.connect('/trinity/local/trix/var/trix.db')
print(c.execute(\"select count(*) from audit where outcome='granted'\").fetchone()[0])")
REFUSALS=$($PY -c "
import sqlite3
c=sqlite3.connect('/trinity/local/trix/var/trix.db')
print(c.execute(\"select count(*) from audit where outcome='refused'\").fetchone()[0])")
[ "${GRANTS:-0}" -gt 0 ] && say PASS "$GRANTS grants recorded" || say FAIL "no grants recorded"
[ "${REFUSALS:-0}" -gt 0 ] && say PASS "$REFUSALS refusals recorded" || say FAIL "no refusals recorded"

echo "=== 11. node tokens ==="
check "anonymous node token refused"   "$(code -X POST $PLAIN/api/v1/auth/node/node001 -H 'Content-Type: application/json' -d '{}')" 401
CRED='{"username":"trix","password":"trix"}'
check "node token on a credential"     "$(code -X POST $PLAIN/api/v1/auth/node/node001 -H 'Content-Type: application/json' -d "$CRED")" 201
NT=$(curl -sk -X POST $PLAIN/api/v1/auth/node/node001 -H 'Content-Type: application/json' -d '{"username":"trix","password":"trix"}' | jq_ "d['token']")
check "a node token is not a user one" "$(code $PLAIN/api/v1/identity/principals -H "x-access-tokens: $NT")" 403

echo "=== 12. replication endpoints ==="
check "table hashes answer"            "$(code $PLAIN/api/v1/system/replication/hashes -H "$H")" 200
check "one table exports"              "$(code $PLAIN/api/v1/system/replication/table/principal -H "$H")" 200
check "triggers report"                "$(code $PLAIN/api/v1/system/triggers -H "$H")" 200
check "renders list"                   "$(code $PLAIN/api/v1/system/renders -H "$H")" 200

echo "=== 13. tidy up ==="
curl -sk -o /dev/null -X PATCH $PLAIN/api/v1/system/acl/genders -H "$H" -H 'Content-Type: application/json' -d '{"usergroups":""}'
curl -sk -o /dev/null -X DELETE $PLAIN/api/v1/identity/principals/e2ereader -H "$H"
curl -sk -o /dev/null -X DELETE $PLAIN/api/v1/identity/usergroups/e2e -H "$H"
say PASS "removed the test organisation"

echo
echo "failures: $FAIL"
exit $FAIL
