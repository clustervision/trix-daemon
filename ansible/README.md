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
`trinityx-combined`, kept here so it stays with the code it installs, and copied
across when the role is added.

Paths below are relative to the root of `trinityx-combined`.

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

The callers that still talk to Luna for what moved, and what changes in each,
are listed in `snippets/consumers.md`.

## trixd.ini, wherever luna.ini is

The daemon's config and every client's copy are called `trixd.ini`: trixd is the
TrinityX daemon. Each sits beside the `luna.ini` it mirrors, with the same owner
and mode, and is written at the same moment in the same role's run.

| Read by | luna.ini | trixd.ini | Owner, mode | Names | Written by |
|---|---|---|---|---|---|
| the daemon | `{{ trix_luna }}/daemon/config/` | `{{ trix_local }}/trix/etc/` | root:root 0640 | itself | `tasks/controller.yml` |
| the command line | `{{ trix_luna }}/cli/config/` | `{{ trix_local }}/trix/cli/config/` | root:root 0640 | this controller | `tasks/cli.yml` |
| the alertx CLI | `{{ trix_local }}/alertx/cli/config/` | the same directory | root:root 0640 | this controller | alertx, through `tasks/alertx.yml` |
| the OOD apps | `{{ trix_ood }}/3.0/config/` | the same directory | root:admins 0640 | `controller.cluster` | ood-apps, through `tasks/ood.yml` |
| the node image | `{{ trix_luna }}/node/config/` | `{{ trix_local }}/trix/node/config/` | root:root 0640 | `controller.cluster` | image-create, through `tasks/nodes.yml` |

`luna2-utils` has a `luna.ini` and gets no `trixd.ini`: none of its tools calls a
route that moved.

Three of the five are written by another role calling into this one with
`include_role`, because the directory belongs to that role:

- **OOD.** `{{ trix_ood }}/3.0` is a symlink to the current apps build, re-pointed
  on every apps upgrade. ood-apps re-renders `luna.ini` straight after the switch,
  and `trixd.ini` has to be written at the same point or it is left behind in the
  old build.
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
ENDPOINT = controller.cluster:8050
PROTOCOL = https
VERIFY_CERTIFICATE = False
```

The account is the one the daemon's own `trixd.ini` names, which is rootus.
Whoever can read a client file acts as rootus: root on a controller or a node,
and the `admins` group in On Demand. Anyone else in On Demand signs in as
themselves (PAM, through sssd, to the cluster directory) and gets the role
rootus gave them with `PUT /api/v1/identity/users/<name>`. No row means role
none, which may read and nothing else. A personal token carries an id Luna never
issued, so Luna refuses it: in Luna's own apps a personal user needs a token from
Luna.

### Where it deliberately differs from luna.ini

- **The daemon's file is in `etc`, not in the package.** `luna.ini` lives inside
  the installed package, so a pip reinstall of Luna replaces it with the default
  it ships. `trixd.ini` is outside, so an upgrade leaves it alone.
- **No `SECRET_KEY` in a client file.** The secret signs tokens for both daemons,
  and a client gains nothing from checking a signature it did not issue.
- **The node file names `controller.cluster`**, as the OOD file does, where the
  node's `luna.ini` names the controller's own hostname. In a pair, the cluster
  name follows the active controller.

## Order matters in two places

**The shared secret must exist before the daemon starts.** By default the daemon
signs with HS256 and the secret at `{{ trix_ssl }}/secret.key`, which `trinity/init`
already writes for `luna.ini` and an HA pair already syncs. Nothing to add: the
role only checks it is there. `snippets/init-keys.yml` applies solely when
`trixd_token_algorithm` is set to `EdDSA`, which Luna cannot yet speak.

**The role runs after `trinity/luna2`.** trix-daemon reads Luna objects by name
at startup to reconcile its renders. Luna does not need trix-daemon at all, so the
dependency points one way only.

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

The password is not among them. It is the role's default, generated once on the
controller that runs the play, as the `ood` role makes its passphrase, so every
`trixd.ini` carries the same one whichever role renders it.

## Decisions to make before this lands

- **Which virtual environment.** The role installs into the shared
  `/trinity/local/python`, which is what every other component does. Nothing in
  trix-daemon's dependency set conflicts with what is already there, checked
  package by package. Giving it its own environment is the alternative, and costs
  duplication.
- **Whether the plain port is exposed.** It is bound to the loopback by default.
  Opening it puts tokens on the cluster network in clear text, and the cluster
  interface is in firewalld's trusted zone, so anything on that network can read
  them.
- **Whether Prometheus service discovery carries a token.** It does not today,
  against Luna or against this daemon, which is why the targets endpoint declares
  itself public. Adding one is a change to the Prometheus configuration as well
  as to the manifest.
- **Which account the node file carries.** It carries the ini account, as the
  node's `luna.ini` carries Luna's administrator. That widens nothing: the node's
  `luna.ini` also holds the shared secret, which can sign a rootus token for
  either daemon. A node only needs a node token, though, and when the cluster does
  not enforce TPM any signed-in account gets one. A dedicated account with role
  none would do, once the secret is out of the image as well.

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
