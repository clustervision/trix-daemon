<!--
This code is part of the TrinityX software suite
Copyright (C) 2026  ClusterVision Solutions b.v.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>
-->

# Consumers that move, and where each one lives

None of these callers has moved yet. This is the list, so the work is visible
rather than discovered halfway through. Every one of them is ours and is
installed by the same installer, which is what makes the move tractable, and
each already has its `trixd.ini` in place beside its `luna.ini` (see the README).

Luna is not changed by any of it. Its routes keep answering; a caller moves when
it is repointed, and until then it reaches Luna exactly as it does today.

## The node hook, after boot

`trinityx-combined`, `site/roles/trinity/alertx/templates/alertx-hook.sh.j2`.

Two changes. The hook reads the node's `trixd.ini` as it reads its `luna.ini`,
with the same parser, and fetches its token from trix-daemon rather than from
Luna. The file is `{{ trix_local }}/trix/node/config/trixd.ini` and names
`{{ trix_ctrl_hostname }}.{{ trix_domain }}:8050` over https.

```sh
# today
curl ... -d "$json" -s "${LUNA_URL}/tpm/${HOST}" > /tmp/token.json
curl ... -H "x-access-tokens: $LUNA_TOKEN" -d "$json" "${LUNA_URL}/import/prometheus_hw_rules"

# after
curl ... -d '{"tpm_sha256":"'$TPM'"}' -s "${TRIX_URL}/api/v1/auth/node/${HOST}" > /tmp/token.json
curl ... -X PATCH -H "x-access-tokens: $TRIX_TOKEN" -d '{"force":false}' \
     "${TRIX_URL}/api/v1/prometheus/hw-rules/${HOST}"
```

When the cluster enforces TPM the measurement is the proof. When it does not,
the hook sends the username and password from the node's `trixd.ini` instead, as
it does with Luna today.

## Node renames and deletes stay with Luna's hook, and trix repairs after it

`luna2-daemon`, `daemon/plugins/hooks/config/node.py`, is not changed. Its rename
and delete still move and remove files under the Prometheus rules directory.
trix-daemon does its own part when a node is renamed or deleted through its
pass-through (`/api/v1/luna/config/node/<name>`): it checks the rule file under
both names, whether Luna moved it or not, and rewrites the `hostname` and
`luna_group` matchers to what Luna now holds. A rename made at Luna directly is
repaired when trix-daemon next starts or a node change next passes through it.
When every caller uses the pass-through, Luna's two shell-outs can go, years
later if need be.

## The On Demand applications

`trinityx-ood`. Each application carries its own copy of a REST client, so a
route change means editing every copy. Each reads
`{{ trix_ood }}/3.0/config/trixd.ini` when it can, which an admin can, and acts as
rootus. Anyone else signs in with their own password and acts with their own
role, which needs a sign-in screen the applications do not have yet.

| Application | File | Today | After |
|---|---|---|---|
| users | `users/luna_requests.py` | eight URLs on `/config/osuser` and `/config/osgroup` | `/api/v1/directory/users`, `.../groups`, real verbs |
| alertx | `alertx/rest.py` | six URLs on `/export/...` and `/import/...` | `/api/v1/prometheus/rules`, `.../settings`, `.../hw-rules` |
| slurm | `slurm/app.py` | writes `/etc/slurm` itself through the generator | the slurm plugin's render writes the blocks from Luna's nodes and groups; editing partitions through the API waits for the generator to take them |

alertx's "save nodes" regenerates nodes' hardware rules, or switches their alerts off,
through Luna's `POST /import/prometheus_hw_rules` with a list of
`{hostname, force, nhc, disabled}`. Its route here is
`PATCH /api/v1/prometheus/hw-rules`, minimum manager, with
`{"nodes": [{"node": ..., "force": ..., "nhc": ..., "disabled": ...}]}`. Each node gets
an answer of its own, a node Luna does not know is reported by name rather than
written, and Prometheus is reloaded once for the lot.

The slurm and genders plugins write the same blocks Luna's node hook still writes, from
the same generator and node list. When that hook stops, they are the only writers, and
nothing else has to change here.

Two shapes the users application depends on. The directory plugin keeps one of
them and deliberately does not keep the other, so this is what changes when
`users/luna_requests.py` is repointed.

The 389ds membership attribute is still renamed to `member`, server side, so the
application's own `normalise_group` becomes a no-op rather than a requirement.

The envelope is gone. Luna answers `{"config": {"osuser": {name: {...}}}}` and the
application unwraps it at two points: `resp.json()['config'][f"os{target[:-1]}"]`
when listing, and the same indexed by name when getting one. trix-daemon answers
the list and the record themselves, because the route already names the object.
Both unwraps come out when the URLs change. A test pins this shape so it cannot
drift back by accident.

The slurm application is the one that is bigger than it looks. It is roughly a
thousand lines and manages three separate managed blocks, one of which it has to
rewrite after the generator runs because the generator's template overwrites it.

## The command line

`luna2-cli`, `luna/utils/rest.py`. Every request builder concatenates one base
URL, so the seam is a second base, read from
`{{ trix_local }}/trix/cli/config/trixd.ini`, plus a base selector in `get_raw`
and `post_raw`. `luna service` and `luna monitor` go through that pair and
nothing else, so moving only those two families needs only the selector.

luna2-cli checks Luna's tokens locally with the `SECRET_KEY` in its `luna.ini`.
`trixd.ini` carries no secret, so the trix side does not check: it uses the
token it was given and signs in again on a 401. A client gains nothing from
checking a signature it did not issue.
