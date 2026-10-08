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

# trixd

The TrinityX control-plane daemon for everything that is not a physical node.
Short name: trixd.

A TrinityX cluster has a controller that runs **Luna** (luna2-daemon), which owns the
physical estate: nodes, groups, images, networks, boot and hardware. trixd is
Luna's peer on the same controller and takes the rest: the cluster's users and
groups, the Prometheus monitoring configuration, starting and stopping services,
and configuration files such as slurm's and genders' that are computed from Luna's
node list but belong to another service.

It is a separate process with its own unit, ports, configuration and database.
It reaches Luna only through Luna's HTTP API, by object name, and Luna is not
changed for it. Neither imports the other.

**The rule that comes before the others: render on change, never on boot.** A
booting node must never depend on trixd being up. Nothing this daemon serves
sits on the boot path.

## Words used here

| Word | Meaning |
|---|---|
| Luna | luna2-daemon, the peer daemon on port 7050 that owns nodes and boot |
| `trixd.ini` | this daemon's configuration file; clients have their own copy |
| ini account | the account `[API] USERNAME` and `PASSWORD` in `trixd.ini` name |
| rootus | what the ini account is: it passes every check. It is never stored as a user |
| principal | a stored user: a name, a password (or none, if PAM or LDAP checks it) and a role |
| role | `none`, `operator` or `manager`, lowest first; a user with no row holds `none` |
| minimum | the lowest role a route admits, written in the route's manifest entry |
| plugin | a directory with a `manifest.yaml` and a backend class; it adds routes under `/api/v1/<plugin>/` |
| manifest | a plugin's declaration of its routes, their minimums, schemas and renders |
| render | an output computed from inputs, such as the Prometheus target list or a slurm file, redone when an input changes |
| declaration | a settings document a plugin stores, kept in the `declaration` table |
| pass-through | `/api/v1/luna/<path>`, which hands the request to Luna unchanged |
| Luna hook | what a plugin does after a node is renamed or deleted through the pass-through (`luna_hooks` in its manifest) |
| node token | a token a booted node gets for itself, confined to that node (Luna's "provision" scope) |
| service token | a short-lived token one controller's trixd gives the other's, for replication |
| replication | how the two controllers of an HA pair keep `principal` and `declaration` the same |
| obol | the command-line tool that manages users in the cluster's 389ds LDAP directory |
| OOD | Open OnDemand, the cluster's web portal, whose TrinityX apps call these APIs |
| alertx | TrinityX's alerting app; its node hook asks for a node's hardware rules after boot |

## How a request moves through the code

**A plugin route**, for example `PATCH /api/v1/prometheus/rules`:

1. `trixd/server.py` serves the app built by `trixd/app.py` on both listeners.
2. The route was built from the plugin's manifest by `trixd/core/plugins/binder.py`.
3. Its gate, `trixd/core/security/gate.py`, verifies the token
   (`core/security/tokens.py`), resolves the caller (`core/security/principals.py`,
   which asks the sources in `core/security/sources/` through `chain.py`), compares
   their role with the route's minimum (`core/security/access.py`) and writes the
   decision to the audit trail (`core/security/audit.py`).
4. The body is validated against the manifest's schema, then the backend method in
   `trixd/plugins/prometheus/backend.py` runs.
5. A return value becomes a JSON 200. A `TrixdError` from `trixd/common/errors.py`
   becomes its own status (400, 401, 403, 404, 409, 502 or 503), a body that fails
   its schema is a 422, and anything else is a 500, with the detail in the log.

**A call to Luna**, for example renaming a node:

1. `trixd/core/luna/proxy.py` forwards `/api/v1/luna/config/node/node001` to Luna as
   `/config/node/node001`, with the method, query, headers and body as sent, less
   the connection headers, and returns Luna's answer unchanged. Luna decides who may.
2. After the answer has left, `trixd/core/luna/hooks.py` looks at what was asked. A
   successful rename, group move or delete is queued as a task (`trixd/core/tasks.py`).
3. The task worker runs every plugin's `luna_hooks` handler for it; the prometheus
   plugin moves the node's rule file and rewrites it to the new name and group.
4. Then the renders that read Luna's nodes wake (`trixd/core/render/engine.py`).

**Startup** (`trixd/core/lifespan.py`): read and check `trixd.ini`, open the database
and bring its schema to the newest Alembic revision, load the signing key, load the
plugins, start the task worker, read Luna's table hashes as a baseline, run every
render once, start the background timers, and only then open the listeners.

## Layout

| Path | What lives there |
|---|---|
| `trixd/app.py` | Builds the application. Reads no configuration. |
| `trixd/server.py` | The entry point: read the ini, serve TLS and plain together. |
| `trixd/common/` | Settings, constants, typed errors, logging. |
| `trixd/core/security/` | Keys, tokens, the sign-in sources and chain, the role gate, the audit trail. |
| `trixd/core/plugins/` | Manifest, loader, registry, router binder, route swap, scaffold. |
| `trixd/core/render/` | The render engine and the Luna table-hash trigger. |
| `trixd/core/replication/` | Keeping two controllers' tables the same. |
| `trixd/core/luna/` | The Luna client, the pass-through and its hooks. |
| `trixd/core/tasks.py` | The queue: one worker, one task at a time, requeued after a restart. |
| `trixd/db/` | Engine, models and the Alembic migrations. |
| `trixd/api/v1/` | The daemon's own routes: auth, identity, system. |
| `trixd/plugins/` | The plugins that ship: directory, prometheus, services, slurm, genders. |
| `trixd/utils/` | Helpers with no daemon state: the shell runner, file writes, declarations. |
| `tests/` | `unit/` for pytest, `e2e/` against a running daemon, `perf/` for timing at scale. |
| `docs/writing-a-plugin.md` | How to write a plugin. |
| `setup.py` | The package itself; see Building and publishing. |

## Run it on a workstation

Python 3.10 or newer.

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pip install -e .
mkdir -p dev
```

Write `dev/trixd.ini`. Comments go on lines of their own: a comment after a value
becomes part of the value.

```ini
[API]
; No CERTFILE and KEYFILE, so only the plain listener opens.
INSECURE_BIND = 127.0.0.1:8051
USERNAME = trixd
PASSWORD = trixd

[TOKENS]
; Any 32 hexadecimal characters, for a workstation.
SECRET_KEY = 004f2af45d3a4e161a7dd2d17fdae47f

[DATABASE]
URL = sqlite:///dev/trixd.db

[LOGGER]
LOGFILE =

[AUTH]
CHAIN = local

[TRIGGERS]
; No Luna here, so the hash comparison is off.
LUNA_HASHES = 0s

[PLUGINS]
DIRECTORY = trixd/plugins
SITE_DIRECTORY = dev/plugins
ENABLED = services, genders
```

Then:

```
python -m trixd.server --config dev/trixd.ini
curl -s http://127.0.0.1:8051/api/v1/system/health
TOKEN=$(curl -s -X POST http://127.0.0.1:8051/api/v1/auth/token \
        -H 'Content-Type: application/json' -d '{"username":"trixd","password":"trixd"}' \
        | python -c 'import sys, json; print(json.load(sys.stdin)["token"])')
curl -s http://127.0.0.1:8051/api/v1/system/plugins -H "x-access-tokens: $TOKEN"
```

A token goes in the `x-access-tokens` header, or as `Authorization: Bearer <token>`.
`http://127.0.0.1:8051/api/v1/docs` lists every route. Without Luna, obol or
Prometheus the plugins still load: a route whose work needs one answers 503, and a
render that could not run answers 404 with the reason.

## On a controller

```
/trinity/local/python/bin/python3 -m trixd.server --config /trinity/local/trixd/etc/trixd.ini
```

The unit `trixd` runs that. `systemctl reload trixd` reloads the plugins
from disk without a restart; a change to `trixd.ini` needs a restart.

| Port | Protocol | Notes |
|---|---|---|
| 8050 | HTTPS | `BIND`; opens only with `CERTFILE` and `KEYFILE`, the cluster certificate pair |
| 8051 | HTTP | `INSECURE_BIND`; the loopback by default |

A token crossing the cluster network in clear text is readable by anything on it,
which is why the plain listener stays on the loopback unless a site opens it.

## trixd.ini

The daemon reads `/trinity/local/trixd/etc/trixd.ini`, or the path given with
`--config` or in `TRIXD_CONFIG`. It lives outside the installed package, so an upgrade
never replaces it. The shipped sample, `trixd/config/trixd.ini`, explains each key. A
missing file, an unknown algorithm or an unreadable duration stops the daemon with
the key named. Durations take `s`, `m`, `h` or `d`; a bare number is seconds,
except `EXPIRY`, where it is hours as in `luna.ini`.

Plugins load from two places: `[PLUGINS] DIRECTORY`, the shipped ones inside the
package, and `SITE_DIRECTORY` (`/trinity/local/trixd/plugins`), a site's own, which an
upgrade leaves alone and which cannot take a shipped plugin's name. A restart loads only
what `ENABLED` lists, by default all five. slurm and genders write the same blocks
luna2-daemon's node hook still writes, from the same generator and node list, so the two
agree; once that hook stops, they are the only writers.

Clients get a `trixd.ini` of their own wherever the installer writes a `luna.ini`:
the command line, the alertx CLI, the On Demand apps and the node image. Each names
where the daemon answers and the ini account. Where each goes, and who can read it,
is the installer's business: the trixd role for trinityx-combined, which is
kept outside this repository until it lands there.

## Who may do what

| Role | May |
|---|---|
| none | read, apart from the routes kept for rootus |
| operator | that, and act: start, stop, restart, reload or query a catalogued service |
| manager | that, and change configuration: rules, settings, a node's hardware rules, the service catalogue |
| rootus | everything, and alone: users and roles, the cluster directory, plugin control, replication, notify, and plugin settings that name a command or a path run as root |

Every user route names its minimum, and the gate compares it with the caller's role
before the handler runs. A refusal is a 403 that says both, as in
`this needs manager; you hold operator`, and goes to the audit trail. The other
controller's service token passes every check.

Users are managed by rootus with `GET /api/v1/identity/users`,
`PUT /api/v1/identity/users/<name>` (create, or change the role, password or whether
the user is enabled) and `DELETE`. A change applies to tokens already handed out:
the role is looked up on each request, through a cache of `[AUTH] CACHE` seconds
(60 by default) that a change clears at once, on this controller and, when the
change arrives there, on the other.

Signing in: the ini account is checked against `trixd.ini` and nothing else.
Anyone else is tried against the sources in `[AUTH] CHAIN` in order, and the first
that knows the name decides: `local` (a stored password), `pam` (the controller's
own accounts, which through sssd include the cluster directory; system accounts are
refused), `ldap` (a directory bind) and `oidc`, which is a placeholder. Someone who
signs in through PAM or LDAP gets a row on first sign-in, with role none.

## Tokens, here and at Luna

`POST /api/v1/auth/token` with a JSON username and password answers 201 with
`{"token": "..."}`, valid for `[API] EXPIRY`. Tokens are HS256 over the secret Luna
signs with too, so either daemon verifies a token the other minted. On a controller
the installer writes that secret into `trixd.ini`, from the value `luna.ini` is
built from.

Luna knows a caller by the token's `id` alone, and reads a missing one as its
administrator, so trixd always sets it:

| Token | `id` | At Luna |
|---|---|---|
| rootus, the ini account | 0 | Luna's administrator, the account `luna.ini` names |
| any other user | -1 | refused, 401: Luna never issues -1 |
| the other controller's service token | -1 | refused, 401 |
| a node token | none | confined to that node by its provision scope |

So a user who is not rootus reaches Luna only with a token Luna issued, and a role
here decides nothing there. Luna's own roles do.

`[TOKENS] ALGORITHM = EdDSA` switches to a keypair, so a daemon that can verify a
token cannot mint one. Luna cannot verify EdDSA yet. Generate the keypair with
`python -m trixd.server keygen --config <path>`.

## Changes made through the route to Luna

`/api/v1/luna/xyz` reaches Luna as `/xyz`, and Luna enforces its own access. Once
Luna has answered, trixd acts on what changed: a node renamed or moved to
another group (`POST config/node/<name>` with `newnodename` or `group`) or deleted
(`GET config/node/<name>/_delete`) is handed to every plugin that declared it. The
prometheus plugin, for one, makes the node's hardware rule file carry the name and
group Luna now holds, whether or not Luna's own hook moved it.

A change made at Luna directly, not through this route, is picked up by comparing
Luna's table hashes every `[TRIGGERS] LUNA_HASHES`. Luna answers that only on the
HA master, so on a single controller such a change reaches the rule files when
trixd next starts, or when the next node change passes through it.

## Writing a plugin

A plugin declares its routes in a manifest. The daemon serves exactly those, and
builds the role gate, the request validation and the published schema from the same
declaration.

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
        return {"message": f"hello {who}"}
```

`python -m trixd.server plugin new hello --config <path>` writes a working one into
`SITE_DIRECTORY`, and rootus plugs it in without a restart; add it to `ENABLED` so a
restart keeps it:

```
curl -X POST http://127.0.0.1:8051/api/v1/system/plugins/hello/reload \
     -H "x-access-tokens: $TOKEN"
```

The guide is [docs/writing-a-plugin.md](docs/writing-a-plugin.md). `luna` is a
reserved plugin name: it is the pass-through.

## Tests

```
pip install pytest httpx
python -m pytest
```

| Kind | Where | What it covers |
|---|---|---|
| API and RBAC | `tests/unit/test_daemon.py` | tokens both ways with Luna, roles on every route, the audit trail, the task queue, the pass-through |
| Plugins | `tests/unit/test_plugins.py` | each shipped plugin's handlers and renders, the scaffold, site plugins |
| Replication | `tests/unit/test_replication.py` | table hashes, the merge, deletions, two controllers converging |
| Integrations | `tests/unit/test_integrations.py` | Luna, the peer, PAM, LDAP, obol, the slurm and genders generators, each stood in for at its edge; the role gate on its own |
| Foundations | `tests/unit/test_foundations.py` | the shell runner, file writes and managed blocks, settings, keys, logging |
| Migrations | `tests/unit/test_migrations.py` | every revision up, down and up again; the models against what the migrations build |
| Command line | `tests/unit/test_entrypoint.py` | listeners, keygen, the plugin scaffold, a bad `trixd.ini` |
| Repository | `tests/unit/test_repository.py` | plain ASCII and the licence header in every file, the version, a real wheel build |

Each test builds its own daemon in a temporary directory, so nothing outside the
checkout is read or written, and the suite runs on Windows as well as Linux.

Timing at scale, with Luna stood in for (the files, database and plugin code are real):

```
python tests/perf/thousand_nodes.py 1000 8
```

End to end, against a running daemon and a real Luna:

| Script | Where it runs | What it needs |
|---|---|---|
| `tests/e2e/on-the-controller.sh` | on the controller | the daemon's `trixd.ini`, naming the account `trixd` with password `trixd` |
| `tests/e2e/over-http.py <host>` | anywhere that reaches 8050 and 8051 | the same account, and port 8051 opened beyond the loopback |
| `tests/e2e/node-rename.sh <node>` | on a development controller only | it renames a node at Luna and back |

The suites sign in with that test account, so they belong on a test controller,
never on a customer's.

## Building and publishing

`.gitlab-ci.yml` includes the shared pip pipeline from `ClusterVision/gitlab-cicd-pipelines`.
It builds every branch with `python setup.py sdist bdist_wheel`, on Python 3.10 and an
old setuptools, which is why the package is declared in `setup.py` and not in
`pyproject.toml`. Two branches are also published to the repository server:

| Branch | Directory, from the CI/CD variable | Installed by |
|---|---|---|
| `development` | `.../trinityx/16.1/trix/noarch/pip/testing/daemon` (`DEV_LOCATION`) | `trix_stream: testing` |
| `main` | `.../trinityx/16.1/trix/noarch/pip/stable/daemon` (`LOCATION`) | `trix_stream: stable` |

A build's version is the release line and the commit's time, for example
`trixd-1.0.1791360000-py3-none-any.whl`. The pipeline points
`trixd-1.0-py3-none-any.whl` at the newest build and keeps the last five. The
installer installs that name with `pip --upgrade`, so a controller takes the newest
build on its next run, and `GET /api/v1/system/version` says which one it is running.

To build as the pipeline does: `python setup.py sdist bdist_wheel`, and `dist/` holds
both files. The release line is `DAEMON_RELEASE` in `trixd/common/constants.py`.
Starting a new one means changing it and the installer's `trixd_pip` together.
