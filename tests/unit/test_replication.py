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
Migrations, the document view of the replicated tables, and the merge.
"""

__author__      = 'Sumit Sharma'
__copyright__   = 'Copyright 2026, TrinityX Project'
__license__     = 'GPL'
__version__     = '1.0'
__maintainer__  = 'Sumit Sharma'
__email__       = 'sumit.sharma@clustervision.com'
__status__      = 'Development'

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from trix.common.constants import API_PREFIX
from trix.core.replication import tables
from trix.db import engine as database
from trix.db import models


def _peer(tmp_path, name='peer.db'):
    """A second, independent database standing in for the other controller."""
    url = f'sqlite:///{tmp_path / name}'
    database.migrate(url)
    handle = sessionmaker(bind=create_engine(url, future=True), future=True)()
    return handle


def test_the_schema_comes_from_a_migration(rig):
    """Not from create_all, so the first schema change has somewhere to go."""
    handle = database.session()
    try:
        stamped = handle.execute(text('select version_num from alembic_version')).scalar()
    finally:
        handle.close()
    assert stamped, 'the database carries no alembic revision'


def test_every_replicated_table_has_a_hash(rig):
    handle = database.session()
    try:
        digests = tables.hashes(handle)
    finally:
        handle.close()
    assert set(digests) == set(tables.ORDER)
    assert all(len(value) == 64 for value in digests.values())


def test_a_write_changes_exactly_one_table_hash(rig):
    handle = database.session()
    try:
        before = tables.hashes(handle)
        handle.add(models.Declaration(plugin='demo', kind='config', name='',
                                      body={'a': 1}, origin='here'))
        handle.commit()
        after = tables.hashes(handle)
    finally:
        handle.close()
    changed = [table for table in tables.ORDER if before[table] != after[table]]
    assert changed == ['declaration']


def test_a_document_is_keyed_by_name_not_by_identifier(rig):
    """Autoincrement is local, so an identifier means nothing to the peer."""
    handle = database.session()
    try:
        handle.add(models.Usergroup(name='operators', origin='here'))
        handle.commit()
        document = tables.export_table(handle, 'usergroup')[0]
    finally:
        handle.close()
    assert document['key'] == ['operators']
    assert 'id' not in document


def test_the_merge_carries_rows_across_and_resolves_the_names(rig, tmp_path):
    """A membership references two rows whose identifiers differ on each side."""
    peer = _peer(tmp_path)
    peer.add(models.Principal(name='alice', kind='user', source='local', origin='peer'))
    peer.add(models.Usergroup(name='operators', origin='peer'))
    peer.commit()
    principal = peer.scalar(select(models.Principal).where(models.Principal.name == 'alice'))
    group = peer.scalar(select(models.Usergroup).where(models.Usergroup.name == 'operators'))
    peer.add(models.Membership(principal_id=principal.id, usergroup_id=group.id,
                               origin='peer'))
    peer.commit()

    handle = database.session()
    try:
        # Something already sits in the local principal table, so the identifiers
        # on the two sides cannot line up by accident.
        for table in tables.ORDER:
            tables.merge_table(handle, table, tables.export_table(peer, table))
        local_principal = handle.scalar(
            select(models.Principal).where(models.Principal.name == 'alice'))
        local_group = handle.scalar(
            select(models.Usergroup).where(models.Usergroup.name == 'operators'))
        link = handle.scalar(select(models.Membership))
        assert local_principal is not None and local_group is not None
        assert link.principal_id == local_principal.id
        assert link.usergroup_id == local_group.id
    finally:
        handle.close()
        peer.close()


def test_the_newer_version_wins_and_the_older_is_ignored(rig, tmp_path):
    peer = _peer(tmp_path, 'peer2.db')
    handle = database.session()
    try:
        handle.add(models.Declaration(plugin='demo', kind='config', name='',
                                      body={'from': 'local'}, version=5, origin='here'))
        handle.commit()

        peer.add(models.Declaration(plugin='demo', kind='config', name='',
                                    body={'from': 'peer'}, version=9, origin='peer'))
        peer.commit()
        tables.merge_table(handle, 'declaration', tables.export_table(peer, 'declaration'))
        row = handle.scalar(select(models.Declaration))
        assert row.body == {'from': 'peer'} and row.version == 9

        older = tables.export_table(peer, 'declaration')
        older[0]['version'] = 2
        older[0]['body'] = {'from': 'stale'}
        tables.merge_table(handle, 'declaration', older)
        row = handle.scalar(select(models.Declaration))
        assert row.body == {'from': 'peer'}, 'an older version overwrote a newer one'
    finally:
        handle.close()
        peer.close()


def test_a_row_whose_dependency_is_missing_is_deferred(rig, tmp_path):
    """It is picked up on a later pass rather than inserted against nothing."""
    peer = _peer(tmp_path, 'peer3.db')
    peer.add(models.Principal(name='bob', kind='user', source='local', origin='peer'))
    peer.add(models.Usergroup(name='admins', origin='peer'))
    peer.commit()
    principal = peer.scalar(select(models.Principal).where(models.Principal.name == 'bob'))
    group = peer.scalar(select(models.Usergroup).where(models.Usergroup.name == 'admins'))
    peer.add(models.Membership(principal_id=principal.id, usergroup_id=group.id,
                               origin='peer'))
    peer.commit()

    handle = database.session()
    try:
        result = tables.merge_table(handle, 'membership',
                                    tables.export_table(peer, 'membership'))
        assert result['deferred'] == 1 and result['added'] == 0
    finally:
        handle.close()
        peer.close()


def test_the_replication_endpoints_answer(client, headers):
    answer = client.get(f'{API_PREFIX}/system/replication/hashes', headers=headers)
    assert answer.status_code == 200
    assert set(answer.json()['hashes']) == set(tables.ORDER)

    answer = client.get(f'{API_PREFIX}/system/replication/table/principal', headers=headers)
    assert answer.status_code == 200
    assert answer.json()['table'] == 'principal'

    answer = client.get(f'{API_PREFIX}/system/replication/table/audit', headers=headers)
    assert answer.status_code == 404, 'audit is local and must not be offered'


def test_the_trigger_state_is_reported(client, headers):
    answer = client.get(f'{API_PREFIX}/system/triggers', headers=headers)
    assert answer.status_code == 200
    assert 'luna_hashes' in answer.json()


def test_a_single_controller_runs_no_sweep(rig):
    """With no peers there is nothing to reconcile with, and no thread."""
    from trix.core.replication.sweep import current as sweep
    assert sweep().start() is False
