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

# Integrating trix-daemon into trinityx-combined

Nothing in this directory is used by the daemon. It is the change set for
`trinityx-combined`, the Ansible installer that builds a TrinityX cluster, kept
here so it stays with the code it installs, and copied across when the role is
added. Paths below are relative to `trinityx-combined`.

## Words used here

| Word | Meaning |
|---|---|
| trixd | trix-daemon, the TrinityX daemon this role installs |
| Luna | luna2-daemon, the other daemon on the controller: nodes, images, boot |
| `trixd.ini` | trixd's configuration file; clients have their own copy, as they have a `luna.ini` |
| ini account | the account `[API] USERNAME` and `PASSWORD` in the daemon's `trixd.ini` name |
| rootus | what the ini account is: it passes every check. It is never stored as a user |
| role | none, operator or manager, held by every other user; no stored user means none |
| minimum | the lowest role a route admits, declared per route |
| OOD | Open OnDemand, the web portal; its TrinityX apps read `luna.ini` and `trixd.ini` |
| primary, secondary | the two controllers of an HA pair; most clusters have one controller |

## What goes where

| What | Where it goes in trinityx-combined | Source here |
|---|---|---|
| The role | `site/roles/trinity/trix-daemon/` | `roles/trix-daemon/` |
| The role invocation | `site/controller.yml`, after `trinity/luna2` | `snippets/controller-role.yml` |
| The OOD apps' trixd.ini | `site/roles/trinity/ood-apps/tasks/trinity.yml` and `templates/login/login-node-ood-prepare-script.j2` | `snippets/ood-apps.yml` |
| The alertx CLI's trixd.ini | `site/roles/trinity/alertx/tasks/cli.yml` | `snippets/alertx.yml` |
| The node image's trixd.ini | `site/roles/trinity/image-create/tasks/{redhat,ubuntu,opensuse}/extend.yml` | `snippets/image-extend.yml` |
| Prometheus discovery | `site/controller.yml`, the prometheus-server block | `snippets/prometheus-sd.yml` |
| Key generation (EdDSA only) | `site/roles/trinity/init/tasks/keys.yml` | `snippets/init-keys.yml` |
| Collecting its Python packages for an offline install | `trinityx-airgap`, `src/airgap_python_packages.py` | add `"trix-daemon": "clustervision/trix-daemon"` |

The callers that still talk to Luna for what moved, and what changes in each,
are listed in `snippets/consumers.md`.

## What the role does on a controller

1. Installs `luna2-python` (the shared Python 3.10 at `/trinity/local/python`) if
   it is missing, and the trix-daemon wheel into it with `pip --upgrade`. Never a
   forced reinstall: that would reinstall every dependency at its newest and move
   the versions Luna pins in the same environment.
2. Links the installed package to `/trinity/local/trix/daemon`, which is where the
   daemon loads its plugins from.
3. In an HA pair, the secondary first takes the API password (and, under EdDSA,
   the keypair) from the primary's sync directory; the primary puts them there.
4. Writes the daemon's `trixd.ini` and the unit, and a `trixd.ini` for the command
   line. A changed package, ini or unit restarts the daemon.
5. Starts it and waits until `GET /api/v1/system/health` answers.

To check an installed controller:

```
systemctl status trix-daemon
curl -s http://127.0.0.1:8051/api/v1/system/health
```

## trixd.ini, wherever luna.ini is

Each copy sits beside the `luna.ini` it mirrors, with the same owner and mode,
and is written at the same moment in the same role's run.

| Read by | luna.ini | trixd.ini | Owner, mode | Names | Written by |
|---|---|---|---|---|---|
| the daemon | `{{ trix_luna }}/daemon/config/` | `{{ trix_local }}/trix/etc/` | root:root 0640 | itself | `tasks/controller.yml` |
| the command line | `{{ trix_luna }}/cli/config/` | `{{ trix_local }}/trix/cli/config/` | root:root 0640 | this controller | `tasks/cli.yml` |
| the alertx CLI | `{{ trix_local }}/alertx/cli/config/` | the same directory | root:root 0640 | this controller | alertx, through `tasks/alertx.yml` |
| the OOD apps | `{{ trix_ood }}/3.0/config/` | the same directory | root:admins 0640 | the controller name | ood-apps, through `tasks/ood.yml` |
| the node image | `{{ trix_luna }}/node/config/` | `{{ trix_local }}/trix/node/config/` | root:root 0640 | the controller name | image-create, through `tasks/nodes.yml` |

"The controller name" is `{{ trix_ctrl_hostname }}.{{ trix_domain }}`: the
floating name that follows the active controller in a pair, and the controller's
own name on a single one. `luna2-utils` has a `luna.ini` and gets no `trixd.ini`:
none of its tools calls a route that moved.

Three of the five are written by another role calling into this one with
`include_role`, because the directory belongs to that role:

- **OOD.** `{{ trix_ood }}/3.0` is a symlink to the current apps build, re-pointed
  on every apps upgrade. ood-apps re-renders `luna.ini` straight after the switch,
  and `trixd.ini` has to be written at the same point or it is left behind in the
  old build. The snippet also writes it when it is missing, so an existing cluster
  gets it without waiting for an apps upgrade.
- **alertx.** `{{ trix_local }}/alertx/cli` is a symlink into site-packages that
  the alertx role makes after installing the CLI. Writing into it first would
  make it a real directory, and the link could no longer be made.
- **The node image.** The image's `luna.ini` is written when image-create
  includes `trinity/luna2` with `extend_image`; including this role the same way
  writes `trixd.ini` beside it and skips the daemon.

### What a client file holds

```ini
[API]
USERNAME = trix
PASSWORD = <generated once, /etc/trinity/passwords/trix/api.txt>
ENDPOINT = <controller name>:8050
PROTOCOL = https
VERIFY_CERTIFICATE = False
```

The account is the ini account, which is rootus. Whoever can read a client file
acts as rootus: root on a controller or a node, and the `admins` group in On
Demand. Anyone else in On Demand signs in as themselves (PAM, through sssd, to
the cluster directory) and gets the role rootus gave them with
`PUT /api/v1/identity/users/<name>`. No row means role none, which may read and
nothing else. A personal token carries an id Luna never issued, so Luna refuses
it: in Luna's own apps a personal user needs a token from Luna.

### Where it deliberately differs from luna.ini

- **The daemon's file is in `etc`, not in the package.** `luna.ini` lives inside
  the installed package, so a pip reinstall of Luna replaces it with the default
  it ships. `trixd.ini` is outside, so an upgrade leaves it alone.
- **No `SECRET_KEY` in a client file.** The secret signs tokens for both daemons,
  and a client gains nothing from checking a signature it did not issue.
- **The node file names the controller name**, as the OOD file does, where the
  node's `luna.ini` names the controller's own hostname.

## Order and secrets

**The role runs after `trinity/luna2`.** trix-daemon reads Luna objects by name
at startup to reconcile what it renders. Luna does not need trix-daemon at all.

**The token secret is Luna's.** The daemon's `trixd.ini` carries `SECRET_KEY`
from `luna_secret_key`, the same fact `luna.ini` is templated from on that
controller, so the two daemons accept each other's tokens by construction. The
role stops if the fact is not set. `snippets/init-keys.yml` applies only when
`trixd_token_algorithm` is `EdDSA`, which Luna cannot verify yet.

**The API password** is generated once, as the `ood` role makes its passphrase,
and an HA pair shares it through the sync directory, so every `trixd.ini` the
role renders carries the same one.

## The variables to add to group_vars

```yaml
trixd_enabled: true
trixd_tls_port: 8050
trixd_plain_port: 8051
trixd_plain_bind: 127.0.0.1        # the loopback, unless you mean to expose it
trixd_token_algorithm: HS256       # what luna2-daemon signs with; EdDSA is the other
trixd_pip: trix_daemon-1.0-py3-none-any.whl
trixd_noarch_pip_repository: "https://updates.clustervision.com/trinityx/{{ trix_version }}/trix/noarch/pip/{{ trix_stream }}"
```

Everything else has a default in `roles/trix-daemon/defaults/main.yml`: the
password, the certificate pair (`ssl_certificate`, else the controller's own
under `ssl_cert_path`), the cluster CA a replication peer is checked against
(`ssl_ca_cert`), and where each client file goes.

## Decisions to make before this lands

- **Which virtual environment.** The role installs into the shared
  `/trinity/local/python`, which is what every other component does. Nothing in
  trix-daemon's pins conflicts with what is there. Giving it its own environment
  is the alternative, and costs duplication.
- **Whether the plain port is exposed.** It is bound to the loopback by default.
  Opening it puts tokens on the cluster network in clear text, and the cluster
  interface is in firewalld's trusted zone, so anything on that network can read
  them.
- **Whether Prometheus service discovery carries a token.** It does not today,
  against Luna or against this daemon, which is why the targets endpoint declares
  itself public.
- **Which account the node file carries.** It carries the ini account, as the
  node's `luna.ini` carries Luna's administrator. That widens nothing: the node's
  `luna.ini` also holds the shared secret, which can sign a rootus token for
  either daemon. A node only needs a node token, though, and when the cluster does
  not enforce TPM any signed-in account gets one. A dedicated account with role
  none would do, once the secret is out of the image as well.

## Offline installs

trinityx-airgap builds the offline wheelhouse from the repositories it lists, and
trix-daemon is not one of them yet. Until it is, an airgapped install cannot find
FastAPI, Hypercorn, SQLAlchemy, Alembic, python-pam or ldap3, none of which ships in
`luna2-python`. The role's own download switches to the mirror through
`vars/airgap/main.yaml`, as every other role's does.

## What is deliberately not here

No command line, and no repointing of the On Demand applications, the alertx CLI
or the node hook. Their `trixd.ini` is in place, so each can move when it is
changed; `snippets/consumers.md` lists what changes in each.

## A note on the firewall

The role opens nothing. Reachability on the cluster network comes from
`trinity/firewalld` putting that whole interface in the trusted zone, which is
also why nothing opens 7050 today. If the daemon has to be reachable from the
public zone, add the explicit rule and the SELinux port label; the
`prometheus-server` role already carries both patterns and is the thing to copy.
