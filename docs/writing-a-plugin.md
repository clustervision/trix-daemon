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

# Writing a plugin for trixd

A plugin declares the endpoints it wants. The daemon serves exactly those, and
builds the permission check, the request validation and the published schema from
the same declaration. A plugin cannot expose an endpoint its manifest does not
name, and no user endpoint can exist without naming the lowest role it admits.

The tables below say what the loader checks. The coding notes under "backend.py"
are conventions the loader does not check. If this page disagrees with the code,
the code is right and this page is a bug. The terms used here are explained in the
README's "Words used here".

## Start from something that loads

```
/trinity/local/python/bin/python3 -m trixd.server plugin new hello --config /trinity/local/trixd/etc/trixd.ini
```

That writes `manifest.yaml`, `backend.py`, `__init__.py` and a README into the
site's plugins directory, `[PLUGINS] SITE_DIRECTORY` (`/trinity/local/trixd/plugins` on
a controller). It loads as it stands, so the first thing you do is see it work and then
edit it into shape.

Plug it in without restarting the daemon. Plugin control needs rootus, so get a
token for the ini account first; on a controller the plain listener is on the
loopback:

```
TOKEN=$(curl -s -X POST http://127.0.0.1:8051/api/v1/auth/token \
        -H 'Content-Type: application/json' -d '{"username":"<ini account>","password":"<its password>"}' \
        | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])')
curl -X POST http://127.0.0.1:8051/api/v1/system/plugins/hello/reload -H "x-access-tokens: $TOKEN"
```

Two things to know before you rely on it:

- **Where a plugin lives decides whether it survives an upgrade.** The shipped plugins
  are in `[PLUGINS] DIRECTORY`, inside the installed package, which an upgrade
  replaces. A site's own go in `SITE_DIRECTORY`, which an upgrade leaves alone. A site
  plugin cannot take a shipped plugin's name: it is skipped, with a warning in the log.
- **A restart loads only the plugins `[PLUGINS] ENABLED` lists.** Add yours there, or a
  plugin plugged in by hand is gone after the next restart.

## The two files

A plugin is a directory holding `manifest.yaml` and a backend module. A single
Python script also loads, if it defines a class named `Plugin` with an `endpoints`
list of the same entries a manifest has (and optionally `name`, `schemas` and
`luna_hooks`); its methods are the handlers. A Luna plugin, which has methods and no
`endpoints`, is refused until that list is added.

### manifest.yaml

| Field | Meaning | Required |
|---|---|---|
| `plugin` | the name, which must match the directory | yes |
| `backend` | the module holding the `Backend` class, default `backend` | no |
| `description` | one or two lines, for whoever reads the manifest | no |
| `schemas` | request bodies, each a set of fields with a type, built once at load | no |
| `endpoints` | what to serve | yes |
| `luna_hooks` | node changes made through the pass-through that the plugin acts on | no |

Each endpoint:

| Field | Meaning | Required |
|---|---|---|
| `path` | under `/api/v1/<plugin>/`, with `<name>` for a variable | yes |
| `methods` | any of `GET`, `POST`, `PATCH`, `DELETE` | no, default `[GET]` |
| `minimum` | the lowest role admitted: `none`, `operator`, `manager`, or `rootus` for the ini account only | yes, unless `public` or `token: node` |
| `handler` | the method on the backend that answers | yes, unless `render` |
| `schema` | a name from `schemas`; the body is validated against it before the handler runs | no; only POST and PATCH carry a body |
| `token` | `user`, or `node` to accept only that node's own token | no |
| `public` | no token at all | no |
| `render` | a derived output; see "Renders" | no |
| `summary` | one line for the published document | no |

### backend.py

One class named `Backend`, one method per declared handler. By convention every
method has annotated parameters and return type, and a docstring that opens and
closes on its own line.

```python
from trixd.common.errors import NotFound

PLUGIN: str = "hello"


class Backend:
    """
    What this plugin is for.
    """

    def get_thing(self, name: str, caller=None) -> dict:
        """
        One thing, or 404.
        """
        if name != "known":
            raise NotFound(f"no thing named {name}")
        return {"name": name, "asked_by": caller.name}
```

A handler is given only the arguments it declares:

- a path variable, under its own name;
- `body`, the validated request body as a dict;
- `caller`, who is asking: a `trixd.core.security.gate.Caller`, with `.name`,
  `.role`, `.kind` (`user`, `node` or `service`) and `.rootus`.

What the handler returns becomes a JSON 200. To answer anything else, raise one of
the errors in `trixd/common/errors.py` (`BadRequest` 400, `NotFound` 404,
`BackendFailure` 502, `ServiceUnavailable` 503 and the rest); any other exception
becomes a 500, with the detail in the log.

**Never a mutable default.** `def act(self, data: dict = {})` creates one
dictionary at definition time and shares it across every call for the life of the
process. Write `data: dict = None`.

## What gets you refused

The loader refuses a plugin as a whole rather than serving half of it. On a
refusal the plugin already serving keeps serving, routes and renders alike, and
the reload endpoint answers 409 with the reason.

| Check | Outcome |
|---|---|
| the plugin is named `luna` | refused; that name is the pass-through to Luna |
| the manifest calls itself something other than its directory | refused |
| a user endpoint declares no minimum role, or one not on the ladder | refused |
| an endpoint names a handler the backend does not have | refused |
| a schema does not parse, or a verb is not one of the four | refused |
| a render names no producer on the backend, or a `coalesce` that is not a duration | refused |
| a `luna_hooks` entry names an unknown change or a missing handler | refused |
| a path is already served by another plugin | refused, naming the other |
| a script's `Plugin` class declares no endpoints | refused; it serves nothing |

## Permissions

Every user holds one role on a ladder, `none < reader < operator < manager`, and every user
endpoint declares the lowest role it admits. The gate compares the two and nothing
else. A path that is read at one level and changed at another is two endpoints:

```yaml
  - path: rules
    methods: [GET]
    minimum: reader
    handler: rules
  - path: rules
    methods: [PATCH]
    minimum: manager
    handler: rules
    schema: rules
```

As a rule, a read admits `reader`, a `POST` under `.../actions/...` admits
`operator`, and a change of configuration needs `manager`. A setting that names a
command the daemon runs, or a path it writes, needs `rootus`: the daemon runs as
root. The ini account is rootus, and a route with `minimum: rootus` admits it
alone, apart from the other controller's service token, which passes every check.
Everyone else holds what the `principal` table says, and a user with no row is
`none`, which may sign in and read nothing. A unit test refuses a shipped plugin
whose read admits `none`. rootus gives a user a role:

```
curl -X PUT http://127.0.0.1:8051/api/v1/identity/users/harry \
     -H "x-access-tokens: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"role": "operator"}'
```

## Acting on a change at Luna

A call made through `/api/v1/luna/...` is forwarded unchanged, and once Luna has
answered 2xx the daemon looks at what was asked. A plugin that wants to act on a
node being renamed, moved between groups or deleted declares it:

```yaml
luna_hooks:
  node.update: node_updated
  node.delete: node_deleted
```

The handler runs on the task queue, after the caller has its answer, with the
payload as keyword arguments: `node_updated(name, newnodename="", group="")`
and `node_deleted(name)`. While the handlers run no render can run, and once they
have all run, the renders that read the object are woken, so a reconcile never
races a handler that is moving a file. A handler must be idempotent: Luna's own
hooks may already have done part of the work.

Only calls made through the pass-through are seen this way. A change made at Luna
directly reaches a plugin through its renders, when Luna's table hashes show it:
on an HA master within `[TRIGGERS] LUNA_HASHES`, and on a single controller only
when trixd next starts.

## Shelling out

Every subprocess goes through `trixd.utils.shell`: an argument list, never a shell
string, always a timeout, and the exit code returned. A name that arrives in a
request is one argument and can never become a command. The value after
`--password` and similar flags is hidden in the log.

```python
from trixd.utils import shell

result = shell.run(["obol", "-J", "user", "show", name], timeout=20)
if not result.ok:
    raise NotFound(f"no user named {name}")
```

## Storing something

A plugin's settings go in the `declaration` table, keyed by plugin, kind and name,
as a document. It is copied to the other controller of an HA pair.

```python
from trixd.utils import declarations

settings = declarations.read(PLUGIN, "settings", "", {"interval": "60s"})
declarations.write(PLUGIN, "settings", "", body)
```

## Renders

A render is an output computed from inputs, such as the Prometheus target list or
a slurm file. The daemon redoes it when an input changes and answers every later
poll from what it last produced.

```yaml
  - path: renders/targets
    methods: [GET]
    minimum: reader
    render:
      inputs:   [luna.node, luna.network]
      coalesce: 2s
      serve:    json
```

| Field | Meaning |
|---|---|
| `inputs` | what wakes it: a Luna object (`luna.node`, `luna.group`, `luna.network`), or the name of one of this plugin's own declarations when a new one arrives from the other controller |
| `coalesce` | how long a burst of changes is gathered, default `2s` |
| `serve` | `json` to answer GET with what was produced; without it the render produces files and serves nothing |

The backend provides `render_<path with dashes and slashes as underscores>`, or
whatever `handler` names, taking no arguments. Every render runs once at startup.
A change wakes it at once, and changes landing within the next `coalesce` wake it
once more when that window closes, so the last one is never lost. Renders run one
at a time. A render that fails keeps its previous output and records the error,
which `/api/v1/system/health` reports.

Writing files is the producer's own job. Write through `trixd.utils.files.write` or
`write_yaml`, which go through a temporary file and a rename, so no reader sees a
half-written file; and produce the same bytes from the same inputs, so two
controllers agree without talking to each other.

## Queued work

Anything that outlives its request goes on the queue, so a slow command never holds
a request. There is one worker for the whole daemon, shared by every plugin and the
Luna hooks, and it runs one task at a time; a task left unfinished by a restart is
run again at the next start.

```python
from trixd.core import tasks


def run_task(task: str, param: str) -> str:
    """
    What the worker runs: return a message, or raise to mark the task failed.
    """
    return f"{task} {param} done"


tasks.register(PLUGIN, run_task)                         # at import
identifier = tasks.submit(PLUGIN, action, param, caller.name)
```

The caller polls whatever route you declare for it; the services plugin uses
`status/<request_id>`.

## Before you call it done

- `trixd plugin check <directory>` prints `would load`. It needs no running daemon and
  no `trixd.ini`, so it fits a pipeline: it parses the manifest, imports the backend,
  and checks every handler and hook exists and takes the path variables its route has.
  It exits 1 and lists every problem otherwise.
- It loads, and unplugs and plugs back with the daemon running.
- Every user endpoint declares a minimum role, and the unit tests read every
  shipped manifest to assert it.
- No path collides with another plugin's.
- The backend has a test that does not need a controller: stub `shell` and Luna.
- Nothing has a mutable default.
