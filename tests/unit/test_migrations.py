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
Every migration, both ways, and the models against what the migrations build.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from trixd.db import engine as database
from trixd.db import models


def _config(url: str) -> Config:
    """
    The Alembic configuration the daemon builds for itself, against this database.
    """
    config = Config()
    config.set_main_option("script_location", database.MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _tables(url: str) -> set:
    """
    The tables in the database, without Alembic's own.
    """
    engine = create_engine(url, future=True)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def test_the_history_has_one_head():
    """
    Two heads would mean two people branched the schema and nobody merged it.
    """
    heads = ScriptDirectory.from_config(_config("sqlite://")).get_heads()
    assert len(heads) == 1, heads


def test_every_revision_upgrades_downgrades_and_upgrades_again(tmp_path):
    """
    One step at a time, so a broken downgrade is named by the revision it is in.
    """
    url = f"sqlite:///{tmp_path / 'stairs.db'}"
    config = _config(url)
    script = ScriptDirectory.from_config(config)
    revisions = list(reversed(list(script.walk_revisions())))
    assert revisions, "no migrations found"
    for revision in revisions:
        command.upgrade(config, revision.revision)
        command.downgrade(config, revision.down_revision or "base")
        command.upgrade(config, revision.revision)
    assert _tables(url) == set(models.Base.metadata.tables)


def test_the_whole_history_goes_down_to_nothing_and_back(tmp_path):
    """
    A downgrade to base leaves no table behind, and head is reachable again.
    """
    url = f"sqlite:///{tmp_path / 'round.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert _tables(url) == set()
    command.upgrade(config, "head")
    assert _tables(url) == set(models.Base.metadata.tables)


def test_the_models_are_what_the_migrations_build(tmp_path):
    """
    A column added to a model without a migration, or the other way round, shows here.
    """
    url = f"sqlite:///{tmp_path / 'compare.db'}"
    database.migrate(url)
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"render_as_batch": True})
            differences = compare_metadata(context, models.Base.metadata)
    finally:
        engine.dispose()
    assert differences == [], differences


def _declaration_row(engine, name: str) -> str:
    """
    An insert naming only the columns the table has right now, so a downgraded table is
    written with its own shape rather than the shape it used to have.
    """
    held = [column["name"] for column in inspect(engine).get_columns("declaration")]
    values = {"plugin": "'p'", "kind": "'k'", "name": f"'{name}'", "body": "'{}'",
              "deleted": "0", "version": "1", "origin": "'here'",
              "updated_at": "'2026-10-07 10:00:00'"}
    columns = [column for column in held if column in values]
    return (f"INSERT INTO declaration ({', '.join(columns)}) "
            f"VALUES ({', '.join(values[column] for column in columns)})")


def _duplicate_is_refused(engine) -> bool:
    """
    Whether the database still refuses a second declaration with the same plugin, kind and name.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        with engine.begin() as connection:
            connection.execute(text(_declaration_row(engine, "n")))
    except IntegrityError:
        return True
    return False


def test_the_declaration_key_stays_unique_when_the_marker_is_undone(tmp_path):
    """
    The downgrade rebuilds the table from reflection, which Alembic documents as lossy, and
    this table carries a named composite constraint the principal table did not. Losing it
    makes two rows with one key insertable, and the read then returns whichever comes first.
    """
    url = f"sqlite:///{tmp_path / 'unique.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text(_declaration_row(engine, "n")))
        assert _duplicate_is_refused(engine), "the key was not unique even before the downgrade"

        command.downgrade(config, "-1")

        assert _duplicate_is_refused(engine), \
            "the downgrade dropped the unique key, so one declaration can now have two rows"
    finally:
        engine.dispose()


def test_undoing_the_declaration_marker_drops_the_markers_and_keeps_the_rest(tmp_path):
    """
    The same rule as for a user: without the column a marker would read as a live document
    again, so taking the column away has to take the markers with it.
    """
    url = f"sqlite:///{tmp_path / 'declmarker.db'}"
    config = _config(url)
    command.upgrade(config, "e1f2a3b4c5d6")
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO declaration (plugin, kind, name, body, deleted, version, origin, "
                "updated_at) VALUES "
                "('prometheus', 'exporter', 'kept', '{}', 0, 1, 'here', "
                "'2026-10-07 10:00:00'), "
                "('prometheus', 'exporter', 'gone', '{}', 1, 2, 'here', "
                "'2026-10-07 10:00:00')"))
        command.downgrade(config, "-1")
        with engine.connect() as connection:
            names = [row[0] for row in connection.execute(text(
                "SELECT name FROM declaration ORDER BY name"))]
    finally:
        engine.dispose()
    assert names == ["kept"]


def test_undoing_the_deleted_marker_drops_the_markers_and_keeps_the_users(tmp_path):
    """
    Before the marker, a deleted user had no row. Taking the column away must not turn
    every marker back into a live user.
    """
    url = f"sqlite:///{tmp_path / 'marker.db'}"
    config = _config(url)
    command.upgrade(config, "c9d0e1f2a3b4")
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO principal (name, password_digest, enabled, role, deleted, version, "
                "origin, updated_at) VALUES "
                "('kept', '', 1, 'operator', 0, 1, 'here', '2026-10-07 10:00:00'), "
                "('gone', '', 1, 'none', 1, 2, 'here', '2026-10-07 10:00:00')"))
        command.downgrade(config, "-1")
        with engine.connect() as connection:
            names = [row[0] for row in connection.execute(text("SELECT name FROM principal"))]
    finally:
        engine.dispose()
    assert names == ["kept"]
