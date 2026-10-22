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
| role | `none`, `reader`, `operator` or `manager`, lowest first; a user with no row holds `none` |
| minimum | the lowest role a route admits, written in the route's manifest entry |
| plugin | a directory with a `manifest.yaml` and a backend class; it adds routes under `/api/v1/<plugin>/` |
| manifest | a plugin's declaration of its routes, their minimums, schemas and renders |
| render | an output computed from inputs, such as the Prometheus target list or a slurm file, redone when an input changes |
| declaration | a settings document a plugin stores, kept in the `declaration` table |
| pass-through | `/api/v1/luna/<path>`, which hands the request to Luna unchanged, up to `[API] MAX_BODY` |
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

1. The luna plugin, `trixd/plugins/luna/backend.py`, forwards
   `/api/v1/luna/config/node/node001` to Luna as `/config/node/node001`, at the address
   Luna's own `luna.ini` names, with the method, query, headers and body as sent, less
   the connection headers, and returns Luna's answer unchanged. Luna decides who may.
   The one limit is `[API] MAX_BODY`, which bounds every request this daemon reads and so
   bounds Luna's whole API through here as well: a body past it is answered 413 and never
   forwarded. It defaults to 8 MiB, which is well above anything the TrinityX tooling
   sends; raise it if a site has a Luna call that legitimately exceeds it.
2. After the answer has left, `trixd/plugins/luna/hooks.py` looks at what was asked. A
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
| `trixd/core/render/` | The render engine, and the register of background triggers a plugin fills. |
| `trixd/core/replication/` | Keeping two controllers' tables the same. |
| `trixd/core/tasks.py` | The queue: one worker, one task at a time, requeued after a restart. |
| `trixd/plugins/luna/` | Everything that knows what Luna is: the client, the reader for Luna's own `luna.ini`, the credentials it is reached with, the table-hash trigger, and the hooks a change through the luna plugin runs. |
| `trixd/db/` | Engine, models and the Alembic migrations. |
| `trixd/api/v1/` | The daemon's own routes: auth, identity, system, and `docs.py`, the docs page and the document it shows. |
| `trixd/static/` | What the docs page loads: the TrinityX look, logo and icon, and Swagger UI 5.32.15, so it works without internet. |
| `trixd/plugins/` | The plugins that ship: luna (the route to Luna), directory, prometheus, services, slurm, genders. |
| `trixd/utils/` | Helpers with no daemon state: the shell runner, file writes, declarations. |
| `tests/` | `unit/` for pytest, `e2e/` against a running daemon, `perf/` for timing at scale. |
| `docs/writing-a-plugin.md` | How to write a plugin. |
| `docs/openapi.json` | The whole API with every shipped plugin, and who may call each route (`x-trixd-access`, `x-trixd-minimum`). |
| `docs/manifest.schema.json` | The plugin manifest format, as a JSON Schema. |
| `tools/build_schemas.py` | Writes both from the code; a test fails when they drift. |
| `setup.py` | The package itself; see Building and publishing. |

## Run it on a workstation

Python 3.10 or newer.

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pip install -e .
mkdir -p dev
```

The Luna plugin reads Luna's address, and the secret for the token it presents to Luna,
from Luna's own `luna.ini`. On a workstation there is no Luna, so write a small one,
`dev/luna.ini`, and point at it:

```ini
[API]
ENDPOINT = localhost:7050
PROTOCOL = https
; Any 32 hexadecimal characters, for a workstation.
SECRET_KEY = 004f2af45d3a4e161a7dd2d17fdae47f
```

```
export TRIXD_LUNA_INI=dev/luna.ini
```

Then write `dev/trixd.ini`. Comments go on lines of their own: a comment after a value
becomes part of the value.

```ini
[API]
; No BIND, so only the plain listener on the loopback opens and no certificate is needed.
BIND =
INSECURE_BIND = 127.0.0.1:7071
USERNAME = trixd
PASSWORD = trixd

[DATABASE]
URL = sqlite:///dev/trixd.db

[LOGGER]
LOGFILE =

[AUTH]
CHAIN = local

[PLUGINS]
DIRECTORY = trixd/plugins
SITE_DIRECTORY = dev/plugins
ENABLED = services, genders
```

Then:

```
python -m trixd.server --config dev/trixd.ini
curl -s http://127.0.0.1:7071/api/v1/system/health
TOKEN=$(curl -s -X POST http://127.0.0.1:7071/api/v1/auth/token \
        -H 'Content-Type: application/json' -d '{"username":"trixd","password":"trixd"}' \
        | python -c 'import sys, json; print(json.load(sys.stdin)["token"])')
curl -s http://127.0.0.1:7071/api/v1/system/plugins -H "x-access-tokens: $TOKEN"
```

A token goes in the `x-access-tokens` header, or as `Authorization: Bearer <token>`.
`http://127.0.0.1:7071/api/v1/docs` lists every route, with what it does, what to send,
what comes back and who may call it; **Authorize** there takes a token for **Try it out**.
The page is FastAPI's own, on Swagger UI files shipped in the package, so it loads nothing
from the internet; `trixd/static/trixd.css` gives it the TrinityX look. Without Luna, obol or
Prometheus the plugins still load: a route whose work needs one answers 503, and a
render that could not run answers 404 with the reason.

## On a controller

```
/trinity/local/python/bin/python3 -m trixd.server --config /trinity/local/trixd/daemon/config/trixd.ini
```

The unit `trixd` runs that. `systemctl reload trixd` reloads the plugins
from disk without a restart; a change to `trixd.ini` needs a restart.

| Port | Setting | When Luna's `luna.ini` says `https` | When it says `http` |
|---|---|---|---|
| 7070 | `BIND` | HTTPS on `CERTFILE` and `KEYFILE`, the cluster pair; a missing one stops the start, by name | plain HTTP, no certificate |
| 7071 | `INSECURE_BIND` | plain HTTP, on every address | plain HTTP, on every address |

The protocol is the one `all.yml` chose for Luna (`luna_protocol`), read from Luna's own
`luna.ini`, as Luna's 7050 follows it. Clients always use 7070; only the scheme changes,
and they take it from their own ini. There is no redirect between the two: a client on
the wrong scheme is a misconfiguration to fix, not to hide. The replication link to the
other controller follows the same protocol.

Both ports answer on every address, because nodes call trixd too. 7071 is clear text:
a password or token sent to it is readable on the network it crosses, so anything that
can use https on 7070 should. A site that wants 7071 on the controller alone sets
`INSECURE_BIND = 127.0.0.1:7071`.

## trixd.ini

The daemon reads `/trinity/local/trixd/daemon/config/trixd.ini`, or the path given with
`--config` or in `TRIXD_CONFIG`. The layout follows Luna's: `/trinity/local/trixd` holds
only `daemon`, a link into the shared Python, as `/trinity/local/luna` does, and the
configuration and the database (`daemon/var/trixd.db`) live inside it. Unlike `luna.ini`,
`trixd.ini` is never in the wheel: the wheel ships `trixd.ini.sample`, which explains each
key, so pip writes only the sample and an upgrade leaves the site's `trixd.ini` and its
database as they were. A `plugins` folder appears beside `daemon` only when a site writes
a plugin of its own. Unlike Luna's tree there is no `utils`: no tool reads a `trixd.ini`
from there. A missing file, an unknown algorithm or an unreadable duration stops the daemon with
the key named. Durations take `s`, `m`, `h` or `d`; a bare number is seconds,
except `EXPIRY`, where it is hours as in `luna.ini`.

Everything else a `trixd.ini` gets wrong is loud and not fatal: the daemon names it and
comes up. A file that exists but cannot be read, a mode the daemon has no rights to
among them, is named with the reason, and the daemon then starts on its built-in
defaults rather than on what that file says. A section or a key trixd does not read, a
misspelled `[DATABASE] URL` among them, is named one by one, because the alternative is
a fresh default database and nothing said; the keys trixd reads are the ones it actually
asks the file for, so a setting added to the code is covered without a second list to
remember. `[API] MAX_BODY` takes `K`, `M` and `G` as powers of 1024, with or without a
`B` or `iB` after them, so `8M`, `8MB` and `8MiB` are one ceiling, and
`[REPLICATION] ENABLED` takes yes or no, on or off, true or false, 1 or 0. Either one
unreadable is named along with the value used instead: the default for `MAX_BODY`, and
on for `ENABLED`, since reading it as off would quietly stop an HA pair reconciling. An
empty `[API] USERNAME` or `PASSWORD` is named too, because that account is the one
principal that holds `rootus`: without it the directory, slurm and replication routes
answer 403 to every caller.

**Nothing about Luna is in `trixd.ini`.** Where Luna answers, its protocol, whether
its certificate is checked, and the token secret both daemons share are read from
Luna's own `luna.ini` (`/trinity/local/luna/daemon/config/luna.ini`), and read again
when it changes. An engineer changes Luna in one place and trixd follows. A `trixd.ini`
that still has a `[LUNA]` section or a `[TOKENS] SECRET_KEY` starts as before, with a
warning that names them as ignored.

Plugins load from two places: `[PLUGINS] DIRECTORY`, the shipped ones inside the
package, and `SITE_DIRECTORY` (`/trinity/local/trixd/plugins`), a site's own, which an
upgrade leaves alone and which cannot take a shipped plugin's name. A restart loads only
what `ENABLED` lists, by default all six. An `ENABLED` that names nothing at all is the
exception: it loads every plugin found rather than none, which is what an installed site
already runs on, so the count it enabled is logged at startup. slurm and genders write
the same blocks luna2-daemon's node hook still writes, from the same generator and node
list, so the two agree; once that hook stops, they are the only writers.

Clients get a `trixd.ini` of their own beside the `luna.ini` the installer writes for
the alertx CLI, the On Demand apps and the node image. The Luna CLI and the luna2
utilities get none: they talk to Luna only. Each names where the daemon answers and the
ini account. Where each goes, and who can read it, is the installer's business: the
`trinity/trixd` role in trinityx-combined.

## Who may do what

The ladder is `none < reader < operator < manager`, the same names luna2-daemon uses,
plus rootus, which is the ini account and never a role a user can be given.

| Role | May |
|---|---|
| none | sign in and ask who it is (`GET /api/v1/auth/whoami`), nothing else |
| reader | read everything apart from the routes kept for rootus: the read-only user, for an auditor or anyone who only needs to see |
| operator | that, and act: start, stop, restart, reload or query a catalogued service |
| manager | that, and change configuration: rules, settings, a node's hardware rules, the service catalogue |
| rootus | everything, and alone: users and roles, the cluster directory, plugin control, replication, notify, and plugin settings that name a command or a path run as root |

rootus is the administrator. There is no separate admin role: whoever administers
trixd does it as the ini account.

Every user route names its minimum, and the gate compares it with the caller's role
before the handler runs. A refusal is a 403 that says both, as in
`this needs manager; you hold operator`, and goes to the audit trail. The other
controller is not an exception: it signs in with the account both controllers are
configured with and is judged on that account, as any caller holding it would be.

The whole decision is these lines. The position on the ladder is the rank:

```python
# trixd/common/constants.py
ROLES: tuple = (ROLE_NONE, ROLE_READER, ROLE_OPERATOR, ROLE_MANAGER)
MINIMUMS: tuple = ROLES + (ROLE_ROOTUS,)

# trixd/core/security/access.py
def rank(role: str) -> int:
    return MINIMUMS.index(role) if role in MINIMUMS else 0

def permits(who, minimum: str) -> tuple:
    held = getattr(who, "role", ROLE_NONE) or ROLE_NONE
    if who is not None and is_rootus(who.name):   # the trixd.ini account
        return True, held
    return rank(held) >= rank(minimum), held
```

The gate, `trixd/core/security/gate.py`, calls `permits` once per request, after the
token is verified and the caller is resolved. An unknown role ranks as none.

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
signs in through PAM or LDAP gets a row on first sign-in, with role none, and can
read nothing until rootus gives them a role.

## Tokens, here and at Luna

`POST /api/v1/auth/token` with a JSON username and password answers 201 with
`{"token": "..."}`, valid for `[API] EXPIRY`. trixd signs and verifies those with its
own material, from `[TOKENS]`: a shared secret, `SECRET_KEY` or `SECRET_FILE`, or a
keypair. The secret in Luna's `luna.ini` is a separate one, read by the Luna plugin for
the token it presents to Luna, so there are two - and a token one daemon minted verifies
at the other only where the installer has written the same value into both.

The key ring is read once, when the daemon starts. Changing what trixd signs with
therefore takes a restart; until then it goes on accepting tokens signed with the old
material, whatever `luna.ini` now says.

trixd's own calls to Luna, the ones its plugins make to read nodes and groups, carry a
token trixd signs itself with that secret, as Luna's administrator, `id` 0. No Luna
password is stored for trixd anywhere, and no sign-in call goes to Luna.

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
Luna's table hashes once an hour, which is the poller's own interval and not a
setting. Luna answers that route only on the HA master, so on a single controller
such a change reaches the rule files when trixd next starts, or when the next node
change passes through it.

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
    minimum: reader
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
curl -X POST http://127.0.0.1:7071/api/v1/system/plugins/hello/reload \
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

End to end, a real daemon over HTTP, about 1,500 cases generated mostly from
`docs/openapi.json` (see `tests/e2e/README`):

```
TRIXD_E2E=local python -m pytest tests/e2e                    # a trixd started here, against a stand-in Luna
TRIXD_E2E_URL=http://<controller>:7071 python -m pytest tests/e2e
TRIXD_E2E=local python -m pytest tests/e2e -m stress -s       # sustained reads and a boot storm
```

| Script | Where it runs | What it needs |
|---|---|---|
| `tests/e2e/on-the-controller.sh` | on the controller, as root | the daemon's `trixd.ini`, whose account it signs in as |
| `tests/e2e/node-rename.sh <node>` | on a development controller only | it renames a node at Luna and back |

The suites sign in with a test account, so they belong on a test controller, never on
a customer's.

On each EL release, in containers, on the luna2-python trixd runs on there (podman or
docker, and the internet):

```
bash tests/platforms/run-matrix.sh
```

| Release | Python | SQLite | glibc | Unit | End to end |
|---|---|---|---|---|---|
| Rocky 8.4 | 3.10.12 | 3.26.0 | 2.28 | pass | pass |
| Rocky 8.10 | 3.10.12 | 3.26.0 | 2.28 | pass | pass |
| Rocky 9.0 | 3.10.12 | 3.34.1 | 2.34 | pass | pass |
| Rocky 9.8 | 3.10.12 | 3.34.1 | 2.34 | pass | pass |
| Rocky 10.2 | 3.10.12 | 3.46.1 | 2.39 | pass | pass |
| AlmaLinux 10.2 | 3.10.12 | 3.46.1 | 2.39 | pass | pass |

SQLite is the operating system's, and it is 3.26.0 on every el8 release, so 8.4 stands
for 8.0 as well. The pipeline runs the el8, el9 and el10 jobs on every push and publishes
nothing unless they and the unit and end-to-end suites pass.

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
