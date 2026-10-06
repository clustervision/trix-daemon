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

# trix-daemon

The TrinityX control-plane daemon for everything that is not a physical node.

Luna keeps the physical estate: nodes, groups, images, networks, boot, hardware.
`trix-daemon` is its peer and takes the rest: the directory, monitoring
configuration, generic service control and the configuration files that are
computed from Luna's nodes but belong to another service.

It is a separate process with its own unit, its own ports, its own configuration
and its own store. It reaches Luna only through Luna's API, by object name, and
Luna is not changed for it: trix-daemon notices a change at Luna when the change
passes through its own route to Luna, and from Luna's table hashes where Luna
answers them. Neither imports the other.

## The rule that comes before the others

**Render on change, never on boot.** A booting node must never depend on
`trix-daemon` being up. Nothing this daemon serves sits on the boot path.

## Layout

| Path | What lives there |
|---|---|
| `trix/app.py` | Builds the application. Reads no configuration. |
| `trix/server.py` | The entry point: read the ini, serve TLS and plain together. |
| `trix/common/` | Settings, constants, typed errors, logging. |
| `trix/core/security/` | Keys, tokens, the resolver chain, the role gate. |
| `trix/core/plugins/` | Manifest, loader, registry, router binder, route swap. |
| `trix/core/render/` | The render engine. |
| `trix/core/luna/` | The Luna client and the reserved pass-through. |
| `trix/db/` | Engine and the declarative models. |
| `trix/api/v1/` | The daemon's own endpoints. |
| `trix/plugins/` | The plugins that ship: directory, prometheus, services, slurm, genders. |
| `trix/utils/` | Leaf helpers with no daemon state. |

## Ports

| Port | Protocol | Notes |
|---|---|---|
| 8050 | HTTPS | `BIND`, with the cluster certificate and key |
| 8051 | HTTP | `INSECURE_BIND`, on the loopback by default |

Both are served from one Hypercorn process. A token crossing the cluster network
in clear text is readable by anything on it, which is why the plain listener is
not exposed by default.

`CERTFILE` and `KEYFILE` are the cluster certificate and key, the same pair Luna
serves on 7050. The Ansible role templates them from `ssl_certificate` and
`ssl_certificate_key`, which is where Luna's own `gunicorn.py` gets them, so the
two daemons cannot drift onto different certificates.

## trixd.ini

The configuration file is `trixd.ini`: trixd is the TrinityX daemon. The daemon
reads `/trinity/local/trix/etc/trixd.ini`, or the path given with `--config` or
in `TRIX_CONFIG`. It lives outside the installed package, so an upgrade never
replaces it.

Clients get a `trixd.ini` of their own wherever the installer writes a
`luna.ini`: the command line, the alertx CLI, the On Demand apps and the node
image. Each names where the daemon answers and the account in the daemon's ini.
Where each goes, and who can read it, is in [ansible/README.md](ansible/README.md).

## Running it

```
/trinity/local/python/bin/python3 -m trix.server --config /trinity/local/trix/etc/trixd.ini
```

`/api/v1/system/health` answers without a token. `/api/v1/docs` is the published
document of every route.

## Who may do what

One table holds the users: a name, a password and a role. The roles are a ladder.

| Role | May |
|---|---|
| none | read: every `GET` |
| operator | read, and act: start, stop or query a service |
| manager | read, act, and change configuration and rules |

**rootus** is the account `trixd.ini` names, never a row. It passes every check,
and only it manages users, plugins, replication and notifications. Every user
endpoint names the lowest role it admits, and the gate compares that with the
caller's role before the handler runs. A refusal is a 403 that says both, as in
`this needs manager; you hold operator`, and goes to the audit trail. The role is
read on every request, so a change applies to tokens already handed out.

Users are managed by rootus with `GET /api/v1/identity/users`,
`PUT /api/v1/identity/users/<name>` (create, or change the role, password or
whether the user is enabled) and `DELETE`. Signing in tries the sources in
`[AUTH] CHAIN`: local, pam, ldap and oidc, of which oidc is a placeholder. Someone
who signs in through PAM and has no row yet gets one, with role none.

## Tokens, here and at Luna

Tokens are HS256 over the secret at `/trinity/local/etc/ssl/secret.key`, the same
secret and the same algorithm `luna2-daemon` uses, so either daemon verifies a
token the other minted. The installer writes that secret at bootstrap.

Luna knows a caller by the token's `id` alone, and reads a missing one as its
administrator, so trix-daemon always says:

| Token | `id` | At Luna |
|---|---|---|
| rootus, the account in `trixd.ini` | 0 | Luna's administrator, the account `luna.ini` names |
| any other user | -1 | refused, 401: Luna never issues -1 |
| the peer daemon's service token | -1 | refused, 401 |
| a node token | none | confined to that node by its provision scope |

So a user who is not rootus reaches Luna only with a token Luna issued, and
trix-daemon's role decides nothing there. Luna's own roles do.

Setting `[TOKENS] ALGORITHM = EdDSA` switches to a keypair, so a daemon that can
verify a token cannot mint one. It needs Luna to speak EdDSA too, and today Luna
does not. Generate the keypair with `-m trix.server keygen --config <path>`.

## Changes made through the route to Luna

`/api/v1/luna/xyz` reaches Luna as `/xyz`, and Luna enforces its own access. Once
Luna has answered, trix-daemon acts on what changed: a node renamed or moved to
another group (`POST config/node/<name>` with `newnodename` or `group`) or deleted
(`GET config/node/<name>/_delete`) is handed to every plugin that declared it.
The prometheus plugin, for one, makes the node's hardware rule file carry the
name and group Luna now holds, whether or not Luna's own hook moved it. Nothing
in Luna is changed for this.

## Writing a plugin

A plugin declares the endpoints it wants in a manifest. The daemon serves exactly
those, and builds the permission gate, the request validation and the published
schema from the same declaration. A plugin cannot expose an endpoint the manifest
does not name, and no user endpoint can exist without naming the lowest role it admits.

```yaml
plugin: hello
backend: backend
endpoints:
  - path: greet/<who>
    methods: [GET]
    minimum: none
    handler: greet
```

```python
class Backend:
    """A plugin that greets."""

    def greet(self, who: str) -> dict:
        """Greet whoever was named in the path."""
        return {'message': f'hello {who}'}
```

Drop that in the plugins directory and plug it in without restarting anything:

```
curl -X POST http://127.0.0.1:8051/api/v1/system/plugins/hello/reload \
     -H "x-access-tokens: $TOKEN"
```

`luna` is a reserved plugin name. It is the one-API route to Luna, and
`/api/v1/luna/xyz` is handed to Luna as `/xyz`, byte for byte, both ways.

## Tests

```
/trinity/local/python/bin/python3 -m pytest tests/unit
```

End to end, against a running daemon and a real Luna:

| Script | Where it runs | What it needs |
|---|---|---|
| `tests/e2e/on-the-controller.sh` | on the controller | the daemon's `trixd.ini`, naming the account `trix` with password `trix` |
| `tests/e2e/over-http.py <host>` | anywhere that reaches 8050 and 8051 | the same account |
| `tests/e2e/node-rename.sh` | on a development controller only | it renames a node at Luna and back |

The suites sign in with that test account, so they belong on a test
controller, never on a customer's.
