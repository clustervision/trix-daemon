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

from trix.db import engine as database
from trix.db import models


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
