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
and its own store. It reaches Luna only through Luna's API, by object name. Luna
reaches it only through a notification. Neither imports the other.

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

## Running it

```
/trinity/local/python/bin/python3 -m trix.server --config <path>/trixd.ini
```

Tokens are HS256 over the secret at `/trinity/local/etc/ssl/secret.key`, the same
secret and the same algorithm `luna2-daemon` uses, so a token minted by either
daemon is accepted by the other. The installer writes that secret at bootstrap.

Setting `[TOKENS] ALGORITHM = EdDSA` switches to a keypair, so a daemon that can
verify a token cannot mint one. It needs Luna to speak EdDSA too, and today Luna
does not. Generate the keypair with `-m trix.server keygen --config <path>`.

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
/trinity/local/python/bin/python3 -m pytest
```
