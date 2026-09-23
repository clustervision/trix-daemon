#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# This code is part of the TrinityX software suite
# Copyright (C) 2026  ClusterVision Solutions b.v.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>

"""
The properties the daemon has to keep, rather than the behaviour of one endpoint.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import jwt
import pytest

from trix.common.constants import API_PREFIX, RESERVED_PLUGIN
from trix.common.errors import PluginRefused
from trix.core.plugins import loader
from trix.core.plugins.manifest import parse
from trix.core.security import tokens


def _serves(app, path: str) -> bool:
    """Whether the route table carries this exact path."""
    return any(getattr(route, 'path', None) == path for route in app.router.routes)


def test_health_needs_no_token(client):
    """A health check that needs a token is not a health check."""
    answer = client.get(f'{API_PREFIX}/system/health')
    assert answer.status_code == 200
    assert answer.json()['status'] == 'ok'


def test_token_is_a_jwt_signed_with_our_key(token):
    """Same format as Luna, asymmetric algorithm, key identifier in the header."""
    header = jwt.get_unverified_header(token)
    assert header['alg'] == 'EdDSA'
    assert header['kid'] == 'trix-test'
    claims = tokens.verify(token)
    assert claims.subject == 'tester'
    assert claims.kind == 'user'


def test_a_token_carries_no_permissions(token):
    """The token says who. The store says what."""
    claims = tokens.verify(token).raw
    for forbidden in ('capabilities', 'permissions', 'roles', 'level', 'grants'):
        assert forbidden not in claims


def test_an_endpoint_refuses_without_a_token(client):
    answer = client.get(f'{API_PREFIX}/directory/users')
    assert answer.status_code == 401


def test_every_plugin_endpoint_declares_a_capability(rig):
    """Derived from the loaded manifests: it fails on the first one that forgets."""
    from trix.core.plugins.registry import current as registry
    for entry in registry().plugins.values():
        if entry.manifest is None:
            continue
        for endpoint in entry.manifest.endpoints:
            assert endpoint.capability, f'{entry.name}{endpoint.path} declares none'


def test_the_published_document_matches_the_route_table(rig):
    """Enumerate one, assert the other."""
    document = rig.openapi()
    published = set(document['paths'])
    registered = {route.path for route in rig.router.routes
                  if getattr(route, 'include_in_schema', False)
                  and getattr(route, 'path', '').startswith(API_PREFIX)}
    assert registered - published == set()


def test_luna_is_reserved(rig):
    """No plugin may claim the one-API route to Luna."""
    with pytest.raises(PluginRefused) as raised:
        loader.load(rig, RESERVED_PLUGIN)
    assert RESERVED_PLUGIN in str(raised.value)


def test_a_manifest_without_a_capability_is_refused():
    with pytest.raises(Exception) as raised:
        parse({'plugin': 'x', 'endpoints': [{'path': 'a', 'methods': ['GET'],
                                             'handler': 'a'}]})
    assert 'capability' in str(raised.value)


def test_a_manifest_naming_a_missing_handler_is_refused(rig, tmp_path):
    """Half a plugin is worse than none, so the whole plugin is refused."""
    broken = tmp_path / 'plugins' / 'broken'
    broken.mkdir()
    (broken / 'manifest.yaml').write_text(
        'plugin: broken\nbackend: backend\nendpoints:\n'
        '  - path: a\n    methods: [GET]\n    capability: broken.read\n'
        '    handler: does_not_exist\n', encoding='utf-8')
    (broken / 'backend.py').write_text('class Backend:\n    """Empty."""\n', encoding='utf-8')

    with pytest.raises(PluginRefused) as raised:
        loader.load(rig, 'broken')
    assert 'does_not_exist' in str(raised.value)


def test_a_plugin_plugs_and_unplugs_while_the_daemon_runs(rig, client, headers):
    """The whole point of the loader: no restart, and the document follows."""
    listed = f'{API_PREFIX}/directory/users'
    assert _serves(rig, listed)

    answer = client.post(f'{API_PREFIX}/system/plugins/directory/disable', headers=headers)
    assert answer.status_code == 200
    assert not _serves(rig, listed)
    assert listed not in rig.openapi()['paths']
    assert client.get(listed, headers=headers).status_code == 404

    answer = client.post(f'{API_PREFIX}/system/plugins/directory/reload', headers=headers)
    assert answer.status_code == 200
    assert _serves(rig, listed)
    assert listed in rig.openapi()['paths']


def test_a_refused_reload_changes_nothing(rig, client, headers, tmp_path):
    """The previously loaded plugin keeps serving, and the call says why."""
    listed = f'{API_PREFIX}/services'
    before = len(rig.router.routes)

    manifest = tmp_path / 'plugins' / 'services' / 'manifest.yaml'
    manifest.write_text(manifest.read_text(encoding='utf-8').replace(
        'handler: list_services', 'handler: no_such_method'), encoding='utf-8')

    answer = client.post(f'{API_PREFIX}/system/plugins/services/reload', headers=headers)
    assert answer.status_code == 409
    assert 'no_such_method' in answer.json()['message']
    assert len(rig.router.routes) == before
    assert _serves(rig, listed)


def test_no_path_is_served_twice(rig):
    """Except under the reserved Luna subtree, which is a single catch-all."""
    seen = {}
    for route in rig.router.routes:
        path = getattr(route, 'path', '')
        if not path.startswith(API_PREFIX) or RESERVED_PLUGIN in path:
            continue
        for method in getattr(route, 'methods', set()):
            key = (method, path)
            assert key not in seen, f'{method} {path} is served twice'
            seen[key] = True


def test_a_node_token_is_refused_on_a_user_endpoint(rig, client):
    """A node carries a token for itself and nothing else."""
    node_token = tokens.mint('node001', kind='node', node='node001')
    answer = client.get(f'{API_PREFIX}/directory/users',
                        headers={'x-access-tokens': node_token})
    assert answer.status_code == 403
