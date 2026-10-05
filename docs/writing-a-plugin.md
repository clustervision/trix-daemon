# Writing a plugin for trix-daemon

A plugin declares the endpoints it wants. The daemon serves exactly those, and
builds the permission check, the request validation and the published schema from
the same declaration. A plugin cannot expose an endpoint its manifest does not
name, and no user endpoint can exist without naming the lowest role it admits.

Everything in this page is what the loader checks. If it disagrees with the code,
the code is right and this page is a bug.

## Start from something that loads

```
/trinity/local/python/bin/python3 -m trix.server plugin new hello --config <path>/trix.ini
```

That writes `manifest.yaml`, `backend.py`, `__init__.py` and a README into the
plugins directory the ini names. It loads as it stands, so the first thing you do
is see it work and then edit it into shape.

Plug it in without restarting the daemon:

```
curl -X POST http://<controller>:8051/api/v1/system/plugins/hello/reload \
     -H "x-access-tokens: $TOKEN"
```

## The two files

A plugin is a directory holding `manifest.yaml` and a backend module. A single
Python script with the declaration as a class attribute also works, which is the
shape Luna's plugins have, so a site plugin moves across without being rewritten.

### manifest.yaml

| Field | Meaning | Required |
|---|---|---|
| `plugin` | the name, which must match the directory | yes |
| `backend` | the module holding the `Backend` class, default `backend` | no |
| `description` | one or two lines; it reaches the published document | no |
| `schemas` | request models, built once at load | no |
| `endpoints` | what to serve | yes |

Each endpoint:

| Field | Meaning | Required |
|---|---|---|
| `path` | under `/api/v1/<plugin>/`, with `<name>` for a variable | yes |
| `methods` | `GET`, `POST`, `PATCH`, `DELETE` | yes |
| `minimum` | the lowest role admitted: `none`, `operator`, `manager`, or `rootus` for the ini account only | yes, unless `public` or `token: node` |
| `handler` | the method on the backend that answers | yes, unless `render` |
| `schema` | a name from `schemas`, validated before the handler runs | for POST and PATCH |
| `token` | `user`, or `node` to accept only that node's own token | no |
| `public` | no token at all | no |
| `render` | a derived output: its inputs, triggers and outputs | no |

### backend.py

One class named `Backend`, one method per declared handler, and nothing else that
matters. Every method takes annotated parameters and returns an annotated type,
and its docstring opens and closes on its own line.

```python
class Backend:
    """
    What this plugin is for.
    """

    def list_things(self, caller=None) -> list:
        """
        Every thing this plugin knows about.
        """
        return []
```

A handler is given only the arguments it declares. `caller` is who is asking,
`body` is the validated request model as a dict, and a path variable arrives under
its own name.

**Never a mutable default.** `def act(self, data: dict = {})` creates one
dictionary at definition time and shares it across every call for the life of the
process. Write `data: dict = None`.

## What gets you refused

The loader refuses a plugin as a whole rather than serving half of it. On a
refusal nothing already serving is disturbed, and the reload endpoint answers 409
with the reason.

| Check | Outcome |
|---|---|
| the plugin is named `luna` | refused; that name is the pass-through to Luna |
| the manifest calls itself something other than its directory | refused |
| a user endpoint declares no minimum role, or one not on the ladder | refused |
| an endpoint names a handler the backend does not have | refused |
| a schema does not parse, or a verb is not one of the four | refused |
| a render names no producer on the backend | refused |
| a path is already served by another plugin | refused, naming the other |
| a class declares no endpoints | refused; it serves nothing |

## Permissions

Every principal holds one role on a ladder, `none < operator < manager`, and
every user endpoint declares the lowest role it admits. The gate compares the two
and nothing else. A path that is read at one level and changed at another is two
endpoints:

```yaml
  - path: config
    methods: [GET]
    minimum: none
    handler: config
  - path: config
    methods: [PATCH]
    minimum: manager
    handler: config
    schema: config
```

As a rule, a read admits `none`, a `POST` under `.../actions/...` admits
`operator`, and a change of configuration needs `manager`. The account the ini
names is rootus and is checked against nothing; a route that declares
`minimum: rootus` admits that account alone, which is how luna2-daemon treats
its user, directory, table and journal families. Everyone else is what the
`principal` table says, and a principal with no row or no role is `none`.

```
curl -X PUT http://<controller>:8051/api/v1/identity/users/harry \
     -H "x-access-tokens: $TOKEN" \
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
and `node_deleted(name)`. Once every handler has run, the renders that read the
object are woken, so a reconcile never races a handler that is still moving a
file. A handler must be idempotent: Luna's own hooks may already
have done part of the work, and a render that reconciles against Luna's node
list may have done the rest. Only calls made through the pass-through are seen;
a change made at Luna directly reaches a plugin through its renders instead.

## Shelling out

Every subprocess goes through `trix.utils.shell`: an argument list, never a shell
string, always a timeout, and the exit code returned. A name that arrives in a
request is one argument and can never become a command.

```python
from trix.utils import shell

result = shell.run(["obol", "-J", "user", "show", name], timeout=20)
if not result.ok:
    raise NotFound(f"no user named {name}")
```

## Storing something

A plugin document goes in the declaration store, keyed by plugin, kind and name.
It replicates between controllers.

```python
from trix.utils import declarations

settings = declarations.read(PLUGIN, "settings", "", {"interval": "60s"})
declarations.write(PLUGIN, "settings", "", body)
```

## Renders

A render declares what it reads, what wakes it and where it goes. The daemon
learns about a change once, renders once, and answers every later poll from what
it rendered.

```yaml
  - path: renders/targets
    methods: [GET]
    minimum: none
    render:
      inputs:   [luna.node, luna.network]
      triggers: [luna.hook, luna.hashes]
      serve:    json
```

The backend provides `render_<path with dashes and slashes as underscores>`, or
whatever `handler` names. Four rules the engine enforces: one writer per file, a
burst of changes renders once, rendering twice from the same inputs produces
byte-identical output, and a write goes to a temporary file and is renamed into
place.

## Queued work

Anything that outlives its request goes on the queue, so a slow command does not
hold a worker thread.

```python
from trix.core import tasks

tasks.register(PLUGIN, run_task)              # at import
identifier = tasks.submit(PLUGIN, action, param, who)
```

The caller polls whatever route you declare for it; the services plugin uses
`status/<request_id>`.

## Before you call it done

- It loads, and unplugs and plugs back with the daemon running.
- Every user endpoint declares a minimum role, and a test asserts that for every plugin.
- No path collides with another plugin's.
- The backend has a test that does not need a controller: stub `shell` and Luna.
- Nothing has a mutable default.
