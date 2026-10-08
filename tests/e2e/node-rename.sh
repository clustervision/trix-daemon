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

# Rename one node through trixd's pass-through and back, and check that its
# hardware rule file follows: the filename and every selector inside it, whatever
# Luna's own hook did or did not do to the file first.
#
# This CHANGES LUNA. Run it on a test cluster, with a node whose rename is harmless.
#
#   node-rename.sh <node> [temporary-name]
#
set -u
NODE=${1:?usage: node-rename.sh <node> [temporary-name]}
TEMP=${2:-${NODE}r}
PLAIN=${PLAIN:-http://localhost:8051}
RULES=${RULES:-/trinity/local/etc/prometheus_server/rules}
PY=${PY:-$(systemctl show trixd -p ExecStart --value 2>/dev/null |
           grep -oE '/[^ ]*/bin/python[0-9.]*' | head -1)}
PY=${PY:-$(command -v python3)}
FAIL=0
say() { printf "  %-6s %s\n" "$1" "$2"; [ "$1" = "FAIL" ] && FAIL=$((FAIL+1)); return 0; }
check() { [ "$2" = "$3" ] && say PASS "$1" || say FAIL "$1 (got $2, wanted $3)"; }
jq_() { $PY -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
J='Content-Type: application/json'

T=$(curl -sk -X POST $PLAIN/api/v1/auth/token -H "$J" -d '{"username":"trixd","password":"trixd"}' | jq_ "d['token']")
[ -n "$T" ] || { echo "no token from $PLAIN"; exit 1; }
H="x-access-tokens: $T"
record() { curl -sk $PLAIN/api/v1/luna/config/node/$1 -H "$H" | jq_ "d['config']['node']['$1']['$2']"; }

HOST=$(record $NODE hostname)
GROUP=$(record $NODE group)
[ -n "$HOST" ] || { echo "$NODE is not a node Luna knows"; exit 1; }
FILE=$RULES/trix.hw.$HOST.rules
echo "=== $NODE ($HOST, group $GROUP) -> $TEMP and back ==="

SYNTHETIC=0
if [ ! -f "$FILE" ]; then
  SYNTHETIC=1
  $PY - "$FILE" "$HOST" "$GROUP" <<'PYX'
import sys
import yaml
path, host, group = sys.argv[1:4]
rules = []
for part in ("memory", "disk"):
    selector = 'hostname="%s",luna_group="%s",class="%s",description="test"' % (host, group, part)
    rules.append({"alert": "MissingHardwareComponent",
                  "expr": "absent(lshw_device{%s})" % selector,
                  "labels": {"severity": "warning", "category": "hardware", "nhc": True, "disabled": False},
                  "annotations": {"description": "Hardware component {{ $labels.class }} is missing."}})
with open(path, "w", encoding="utf-8") as handle:
    yaml.safe_dump({"groups": [{"name": "trinityx_hw", "rules": rules}]}, handle)
PYX
  say PASS "wrote a synthetic rule file for $HOST"
fi
cp -a "$FILE" /tmp/node-rename.before
COUNT=$(grep -c 'hostname="' "$FILE")

rename_node() {
  curl -sk -o /dev/null -w '%{http_code}' -X POST $PLAIN/api/v1/luna/config/node/$1 -H "$H" -H "$J" \
       -d "{\"config\":{\"node\":{\"$1\":{\"newnodename\":\"$2\"}}}}"
}
settled() {
  local file=$RULES/trix.hw.$1.rules
  for _ in $(seq 1 30); do
    if [ -f "$file" ] && [ "$(grep -c "hostname=\"$1\"" "$file")" = "$COUNT" ] \
       && ! grep -q "hostname=\"$2\"" "$file" && [ ! -f "$RULES/trix.hw.$2.rules" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

check "rename accepted by Luna" "$(rename_node $NODE $TEMP)" 204
NEWHOST=$(record $TEMP hostname)
check "Luna knows the new name" "$([ -n "$NEWHOST" ] && echo yes)" yes
if settled "$NEWHOST" "$HOST"; then
  say PASS "file and all $COUNT selectors follow to $NEWHOST"
else
  say FAIL "rules did not follow to $NEWHOST within 30s"
  ls -la $RULES/ | grep "$NODE\|$TEMP" | sed 's/^/         /'
fi

check "rename back accepted" "$(rename_node $TEMP $NODE)" 204
if settled "$HOST" "$NEWHOST"; then
  say PASS "file and all $COUNT selectors follow back to $HOST"
else
  say FAIL "rules did not follow back to $HOST within 30s"
fi

if [ $SYNTHETIC = 1 ]; then
  rm -f "$FILE"
  say PASS "removed the synthetic file"
else
  cp -a /tmp/node-rename.before "$FILE"
fi
systemctl reload prometheus-server 2>/dev/null
echo
echo "failures: $FAIL"
exit $FAIL
