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
across when the role is actually added.

Paths below are relative to the root of `trinityx-combined`.

## What goes where

| What | Where it goes in trinityx-combined | Source here |
|---|---|---|
| The role | `site/roles/trinity/trix-daemon/` | `roles/trix-daemon/` |
| The role invocation | `site/controller.yml`, after `trinity/luna2` | `snippets/controller-role.yml` |
| Key generation (EdDSA only) | `site/roles/trinity/init/tasks/keys.yml` | `snippets/init-keys.yml` |
| Prometheus discovery | `site/controller.yml`, the prometheus-server block | `snippets/prometheus-sd.yml` |
| The node hook | `site/roles/trinity/alertx/templates/alertx-hook.sh.j2` | `snippets/alertx-hook.md` |
| Luna's config hook | `luna2-daemon`, `daemon/plugins/hooks/config/node.py` | `snippets/luna-hook.md` |

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

## Three decisions to make before this lands

- **Which virtual environment.** The role as written installs into the shared
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

## What is deliberately not here

No CLI, and no repointing of the On Demand applications. Those are separate steps
in the delivery plan, and both want the daemon in place first. The consumer
changes are listed in `snippets/consumers.md` so the work is visible, not so it
is done now.

## A note on the firewall

The role opens nothing. Reachability on the cluster network comes from
`trinity/firewalld` putting that whole interface in the trusted zone, which is
also why nothing opens 7050 today. If the daemon has to be reachable from the
public zone, add the explicit rule and the SELinux port label; the
`prometheus-server` role already carries both patterns and is the thing to copy.
