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

Nothing here is done yet. It is the list, so the work is visible rather than
discovered halfway through. Every one of these callers is ours and is installed
by the same installer, which is what makes the move tractable.

## The node hook, after boot

`trinityx-combined`, `site/roles/trinity/alertx/templates/alertx-hook.sh.j2`.

Two changes. The hook needs trix-daemon's address next to Luna's, and it fetches
its token from trix-daemon rather than from Luna.

```sh
# today
curl ... -d "$json" -s "${LUNA_URL}/tpm/${HOST}" > /tmp/token.json
curl ... -H "x-access-tokens: $LUNA_TOKEN" -d "$json" "${LUNA_URL}/import/prometheus_hw_rules"

# after
curl ... -d '{"tpm_sha256":"'$TPM'"}' -s "${TRIX_URL}/api/v1/auth/node/${HOST}" > /tmp/token.json
curl ... -X PATCH -H "x-access-tokens: $TRIX_TOKEN" -d '{"force":false}' \
     "${TRIX_URL}/api/v1/prometheus/hw-rules/${HOST}"
```

`TRIX_URL` is rendered next to `LUNA_URL`, from the node's own ini. The node ini
template is `site/roles/trinity/luna2/templates/luna2.ini.j2`, rendered into the
image by that role's `tasks/nodes.yml`.

**This is where the shared secret stops being needed.** trix-daemon verifies the
measurement by asking Luna what it holds for that node, so nothing signing-related
has to reach a node image. That is the point of TRIX-2067.

## Luna's config hook becomes a notifier

`luna2-daemon`, `daemon/plugins/hooks/config/node.py` and `group.py`.

The hook stops doing the work and posts the changed object name instead. Fire and
forget: it does not wait for an answer and does not fail if none comes, because
the hash comparison on the slow timer catches up.

```python
def postupdate(self, name: str = '', group: str = '') -> tuple:
    """Tell trix-daemon a node changed, and return."""
    requests.post(f'{TRIX_URL}/api/v1/system/notify',
                  json={'object': 'node', 'name': name},
                  headers={'x-access-tokens': token}, timeout=2)
    return True, 'notified'
```

Two things in that file move with it rather than staying behind. `rename` and
`delete` currently shell out to rename and remove files under the Prometheus
rules directory, unconditionally, whether or not the config packages import. If
the prometheus plugin is to be the one writer of that directory, those two go
too.

## The On Demand applications

`trinityx-ood`. Each application carries its own copy of a REST client, so a
route change means editing every copy.

| Application | File | Today | After |
|---|---|---|---|
| users | `users/luna_requests.py` | eight URLs on `/config/osuser` and `/config/osgroup` | `/api/v1/directory/users`, `.../groups`, real verbs |
| alertx | `alertx/rest.py` | six URLs on `/export/…` and `/import/…` | `/api/v1/prometheus/rules`, `.../settings`, `.../hw-rules` |
| slurm | `slurm/app.py` | writes `/etc/slurm` itself through the generator | `PATCH /api/v1/slurm/partitions`, and the render writes the files |

Two shapes the users application depends on and that have to survive the move:
the list endpoint answers a list while the single-user endpoint answers an object
keyed by name, and the 389ds membership attribute is renamed to `member`. The
directory plugin keeps both.

The slurm application is the one that is bigger than it looks. It is roughly a
thousand lines and manages three separate managed blocks, one of which it has to
rewrite after the generator runs because the generator's template overwrites it.

## The command line

`luna2-cli`, `luna/utils/rest.py`. Every request builder concatenates one base
URL, so the seam is a second endpoint in the ini plus a base selector in
`get_raw` and `post_raw`. `luna service` and `luna monitor` go through that pair
and nothing else, so moving only those two families needs only the selector.

Both clients currently verify tokens locally by decoding them with the shared
secret. Under asymmetric signing that is a code change, not a configuration
change. The simplest fix is to stop verifying client-side: carry the token, and
fetch a new one when a call answers 401. A client has nothing to gain from
checking a signature it did not issue.

## The compatibility window

For one release the thirteen moved paths stay registered in Luna and answer 410
Gone with a `Location` header naming the new path. Not a silent forwarder: a
forwarder is a copy of the route that drifts, and a consumer nobody repointed
keeps working until the day it does not. The installer repoints everything in the
same change that installs the daemon, so a fresh install never sees a 410 and an
upgraded cluster with a hand-edited caller sees one and knows why.
