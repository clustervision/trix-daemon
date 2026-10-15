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

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from trixd.common.constants import API_PREFIX
from trixd.core.replication import tables
from trixd.db import engine as database
from trixd.db import models


def _peer(tmp_path, name="peer.db"):
    """
    A second, independent database standing in for the other controller.
    """
    url = f"sqlite:///{tmp_path / name}"
    database.migrate(url)
    handle = sessionmaker(bind=create_engine(url, future=True), future=True)()
    return handle


def test_the_schema_comes_from_a_migration(rig):
    """
    Not from create_all, so the first schema change has somewhere to go.
    """
    handle = database.session()
    try:
        stamped = handle.execute(text("select version_num from alembic_version")).scalar()
    finally:
        handle.close()
    assert stamped, "the database carries no alembic revision"


def test_every_replicated_table_has_a_hash(rig):
    """
    A table with no hash would never be compared, and so never reconciled.
    """
    handle = database.session()
    try:
        digests = tables.hashes(handle)
    finally:
        handle.close()
    assert set(digests) == set(tables.ORDER)
    assert all(len(value) == 64 for value in digests.values())


def test_a_write_changes_exactly_one_table_hash(rig):
    """
    A write that moved every hash would make the sweep copy the whole store.
    """
    handle = database.session()
    try:
        before = tables.hashes(handle)
        handle.add(models.Declaration(plugin="demo", kind="config", name="",
                                      body={"a": 1}, origin="here"))
        handle.commit()
        after = tables.hashes(handle)
    finally:
        handle.close()
    changed = [table for table in tables.ORDER if before[table] != after[table]]
    assert changed == ["declaration"]


def test_a_document_is_keyed_by_name_not_by_identifier(rig):
    """
    Autoincrement is local, so an identifier means nothing to the peer.
    """
    handle = database.session()
    try:
        handle.add(models.Principal(name="keyed", origin="here"))
        handle.commit()
        documents = tables.export_table(handle, "principal")
    finally:
        handle.close()
    document = [d for d in documents if d["key"] == ["keyed"]][0]
    assert "id" not in document


def test_the_merge_carries_a_principal_across_by_name(rig, tmp_path):
    """
    The peer's row arrives under a local identifier of its own, with its role intact.
    """
    peer = _peer(tmp_path)
    peer.add(models.Principal(name="alice", role="manager", origin="peer"))
    peer.commit()
    theirs = peer.scalar(select(models.Principal).where(models.Principal.name == "alice"))

    handle = database.session()
    try:
        # A local row takes the first identifier, so the two sides cannot line up by accident.
        handle.add(models.Principal(name="zed"))
        handle.commit()
        tables.merge_table(handle, "principal", tables.export_table(peer, "principal"))
        ours = handle.scalar(select(models.Principal).where(models.Principal.name == "alice"))
        assert ours is not None and ours.role == "manager"
        assert ours.id != theirs.id
    finally:
        handle.close()
        peer.close()


def test_the_newer_version_wins_and_the_older_is_ignored(rig, tmp_path):
    """
    Last writer wins by version, then by timestamp.
    """
    peer = _peer(tmp_path, "peer2.db")
    handle = database.session()
    try:
        handle.add(models.Declaration(plugin="demo", kind="config", name="",
                                      body={"from": "local"}, version=5, origin="here"))
        handle.commit()

        peer.add(models.Declaration(plugin="demo", kind="config", name="",
                                    body={"from": "peer"}, version=9, origin="peer"))
        peer.commit()
        tables.merge_table(handle, "declaration", tables.export_table(peer, "declaration"))
        row = handle.scalar(select(models.Declaration))
        assert row.body == {"from": "peer"} and row.version == 9

        older = tables.export_table(peer, "declaration")
        older[0]["version"] = 2
        older[0]["body"] = {"from": "stale"}
        tables.merge_table(handle, "declaration", older)
        row = handle.scalar(select(models.Declaration))
        assert row.body == {"from": "peer"}, "an older version overwrote a newer one"
    finally:
        handle.close()
        peer.close()


def test_the_replication_endpoints_answer(client, headers):
    """
    The peer reads these, so they must answer on a controller with no peer.
    """
    answer = client.get(f"{API_PREFIX}/system/replication/hashes", headers=headers)
    assert answer.status_code == 200
    assert set(answer.json()["hashes"]) == set(tables.ORDER)

    answer = client.get(f"{API_PREFIX}/system/replication/table/principal", headers=headers)
    assert answer.status_code == 200
    assert answer.json()["table"] == "principal"

    answer = client.get(f"{API_PREFIX}/system/replication/table/audit", headers=headers)
    assert answer.status_code == 404, "audit is local and must not be offered"


def test_a_peer_daemon_reaches_the_replication_endpoints(client):
    """
    A peer signs in here and presents the token this daemon issued it, which is the ini
    account and so rootus. Nothing is minted for a peer: the two controllers of a pair
    generate their own secret, so only the issuer can verify what it issued.
    """
    from trixd.common.constants import TOKEN_HEADER
    from trixd.core.security import tokens

    peer = {TOKEN_HEADER: tokens.mint("tester")}
    for path in ("hashes", "table/principal"):
        answer = client.get(f"{API_PREFIX}/system/replication/{path}", headers=peer)
        assert answer.status_code == 200, answer.text


def test_the_trigger_state_is_reported(client):
    """
    Whether the fallback poller is doing anything, which on one controller it is not.
    """
    answer = client.get(f"{API_PREFIX}/system/health")
    assert answer.status_code == 200
    assert "answering" in answer.json()["triggers"]["luna_hashes"]


def test_a_single_controller_runs_no_sweep(rig):
    """
    With no peers there is nothing to reconcile with, and no thread.
    """
    from trixd.core.replication.sweep import current as sweep
    assert sweep().start() is False


def test_one_table_carries_the_whole_of_rbac(rig):
    """
    A user, a password, a role, as the whiteboard drew it: no groups, flags or modes. The
    rest is bookkeeping so the row and its deletion reach the peer.
    """
    from trixd.db import models

    columns = {c.name for c in models.Principal.__table__.columns}
    assert columns == {"id", "name", "password_digest", "enabled", "role", "deleted",
                       "version", "origin", "updated_at"}, columns
    for gone in ("Usergroup", "Membership", "UsergroupMap", "Permission"):
        assert not hasattr(models, gone), gone
    assert models.REPLICATED == ("declaration", "principal")


def test_the_declaration_carries_no_permissions(rig):
    """
    What a caller may do is decided per route, never per document.
    """
    from trixd.db import models

    columns = {c.name for c in models.Declaration.__table__.columns}
    assert not {"owners", "usergroups", "access"} & columns, columns


def test_every_replicated_column_reaches_the_document(rig):
    """
    A column the export leaves out never crosses to the peer, silently.
    """
    from trixd.core.replication import tables
    from trixd.db import engine as database
    from trixd.db import models

    handle = database.session()
    try:
        # Something in every table, so each one has a row to export.
        handle.add_all([
            models.Principal(name="peered", role="manager"),
            models.Declaration(plugin="peer", kind="config", name="", body={}),
        ])
        handle.commit()

        for table in models.REPLICATED:
            model = tables.MODELS[table]
            columns = {c.name for c in model.__table__.columns}
            # id is local and a foreign key becomes a name in the key.
            expected = {c for c in columns
                        if c != "id" and not c.endswith("_id")}
            documents = tables.export_table(handle, table)
            assert documents, f"{table} exported nothing to check"
            carried = set(documents[0])
            missing = expected - carried
            assert not missing, f"{table} does not carry {sorted(missing)}"
    finally:
        handle.close()


def test_a_role_survives_the_round_trip(rig):
    """
    The role is the whole of RBAC, and it crosses as a document or not at all.
    """
    from trixd.core.replication import tables
    from trixd.db import engine as database
    from trixd.db import models

    handle = database.session()
    try:
        handle.add(models.Principal(name="traveller", role="operator"))
        handle.commit()
        documents = tables.export_table(handle, "principal")
    finally:
        handle.close()
    crossing = [d for d in documents if d["key"] == ["traveller"]]
    assert crossing, documents
    assert crossing[0]["role"] == "operator"


def test_two_controllers_converge_and_stay_converged(rig, tmp_path):
    """
    The peer's timestamp travels with the row, so after one exchange each way the hashes
    agree and a further pass copies nothing.
    """
    peer = _peer(tmp_path)
    peer.add(models.Principal(name="alice", role="manager", origin="peer"))
    peer.commit()
    handle = database.session()
    try:
        tables.merge_table(handle, "principal", tables.export_table(peer, "principal"))
        tables.merge_table(peer, "principal", tables.export_table(handle, "principal"))
        assert tables.table_hash(handle, "principal") == tables.table_hash(peer, "principal")
        again = tables.merge_table(handle, "principal", tables.export_table(peer, "principal"))
        assert again == {"added": 0, "updated": 0, "conflicts": 0}
    finally:
        handle.close()
        peer.close()


def test_a_deletion_reaches_the_peer_instead_of_coming_back(rig, client, headers, tmp_path):
    """
    A deleted user stays as a marker with a newer version, so the peer drops them too.
    """
    from trixd.common.constants import API_PREFIX

    client.put(f"{API_PREFIX}/identity/users/bob", json={"role": "manager"}, headers=headers)
    handle = database.session()
    peer = _peer(tmp_path)
    try:
        tables.merge_table(peer, "principal", tables.export_table(handle, "principal"))
        assert client.delete(f"{API_PREFIX}/identity/users/bob",
                             headers=headers).status_code == 200
        handle.expire_all()
        tables.merge_table(peer, "principal", tables.export_table(handle, "principal"))
        tables.merge_table(handle, "principal", tables.export_table(peer, "principal"))
        theirs = peer.scalar(select(models.Principal).where(models.Principal.name == "bob"))
        assert theirs.deleted and theirs.role == "none"
        listed = client.get(f"{API_PREFIX}/identity/users", headers=headers).json()["users"]
        assert "bob" not in [user["name"] for user in listed]
    finally:
        handle.close()
        peer.close()


def test_a_declaration_from_the_peer_wakes_the_renders_that_read_it(rig, tmp_path, monkeypatch):
    """
    A plugin's settings arriving by replication render here, as a change made here would.
    """
    from trixd.core.render.engine import current as renders

    woken = []
    monkeypatch.setattr(renders(), "notify", lambda changed="": woken.append(changed) or [])
    peer = _peer(tmp_path)
    peer.add(models.Declaration(plugin="prometheus", kind="rules", name="",
                                body={"groups": []}, origin="peer"))
    peer.commit()
    handle = database.session()
    try:
        tables.merge_table(handle, "declaration", tables.export_table(peer, "declaration"))
    finally:
        handle.close()
        peer.close()
    assert woken == ["prometheus.rules"]

def test_a_newer_call_for_one_target_replaces_the_older_row(rig):
    """
    The table is the size of the distinct things that changed, not of the call volume. A
    reboot storm sends the same body per node, so repeats of one node collapse; and because
    only the newest intent survives, no stale row can be applied late and undo newer state.
    """
    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    for _ in range(5):
        outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                      {"node": "node001"}, {"force": True}, "req", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                  {"node": "node002"}, {"force": True}, "req", "tester")
    # A delete for the same node is a different intent and must not merge with the patch.
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                  {"node": "node001"}, None, "req", "tester")

    handle = database.session()
    try:
        rows = handle.scalars(select(models.Outbox)).all()
        assert len(rows) == 3, [(r.method, r.target) for r in rows]
        assert {(r.method, r.target.split("|")[0]) for r in rows} == {
            ("PATCH", "node=node001"), ("PATCH", "node=node002"),
            ("DELETE", "node=node001")}
    finally:
        handle.close()


def test_a_rename_never_collapses_with_another_rename(rig):
    """
    Two renames of one node are not the same intent: the second's `was` is the first's new
    name, so dropping the first would leave the file under a name nothing looks for. The
    body digest in the key makes that fall out of the schema rather than needing a case.
    """
    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/rename", "POST",
                  {"node": "node002"}, {"was": "node001"}, "req", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/rename", "POST",
                  {"node": "node003"}, {"was": "node002"}, "req", "tester")
    handle = database.session()
    try:
        assert len(handle.scalars(select(models.Outbox)).all()) == 2
    finally:
        handle.close()


def test_a_dark_peer_stops_being_attempted_after_three_failures(rig, monkeypatch):
    """
    TRIX-2049's shape, pre-empted: one worker, a bounded threadpool, and a dark peer would
    otherwise have every replicated call hold a thread for the timeout while every other
    caller queues behind it. Three consecutive transport failures say the peer rather than
    the call, and from then on a replicated call costs a row and no socket at all.
    """
    from trixd.core.replication import outbox

    tried = []

    def refuse(peer, row):
        """
        A peer that is not answering.
        """
        tried.append(peer)
        return outbox.QUEUED, "connection refused"

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    original, outbox._send = outbox._send, refuse
    outbox._failures.clear()
    outbox._open.clear()
    try:
        for _ in range(10):
            said = outbox.replicate("prometheus", "hw-rules/<node>", "DELETE",
                                    {"node": "node001"}, None, {"retired": []},
                                    "req", "tester")
        assert len(tried) == 3, f"attempted a dark peer {len(tried)} times, not 3"
        assert said["peers"] == {"ctrl2": "peer-down"}, said
        assert outbox.breaker_open("ctrl2")

        # Only a sweep reaching the peer closes it; nothing probes from the request path.
        outbox.record_success("ctrl2")
        assert not outbox.breaker_open("ctrl2")
    finally:
        outbox._send = original
        outbox._failures.clear()
        outbox._open.clear()


def test_a_call_that_changed_nothing_is_not_replicated(rig, monkeypatch):
    """
    The local path deliberately costs nothing for a node that rebooted unchanged, and
    replicating anyway would undo that at the one moment it matters. Only an explicit
    falsy `changed` skips, so the default is to send: a missed replication is a silent
    divergence, a redundant one is an idempotent no-op at the peer.
    """
    from trixd.core.replication import outbox

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    sent = []
    original = outbox._send
    outbox._send = lambda peer, row: (sent.append(peer), (outbox.DONE, ""))[1]
    try:
        for result, expected in (({"changed": False}, "skipped"),
                                 ({"changed": 0}, "skipped"),
                                 ({"changed": True}, None),
                                 ({"retired": ["a"]}, None),
                                 (["a", "b"], None)):
            said = outbox.replicate("prometheus", "hw-rules/<node>", "PATCH",
                                    {"node": "node001"}, {}, result, "req", "tester")
            if expected == "skipped":
                assert said.get("state") == "skipped", result
            else:
                assert said["peers"].get("ctrl2") == outbox.DONE, result
        assert len(sent) == 3, "a result without an explicit falsy changed must be sent"
    finally:
        outbox._send = original

def test_the_peer_will_not_be_talked_into_work_its_manifest_does_not_declare(client, headers):
    """
    The flag is checked at both ends. A peer running an older manifest refuses an action for
    an endpoint it does not declare, which is what a version-skewed pair should do instead of
    being persuaded into writes nobody marked as crossing. The refusal is terminal, so the
    sender stops rather than retrying for a day.
    """
    from trixd.common.constants import API_PREFIX

    refused = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": "directory", "path": "users/<name>", "method": "PATCH",
        "variables": {"name": "someone"}, "body": {}, "origin": "ctrl1", "request_id": "r1"})
    assert refused.status_code == 400, refused.text
    assert "replicate" in refused.json()["message"]

    missing = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": "nosuch", "path": "x", "method": "PATCH", "origin": "ctrl1"})
    assert missing.status_code == 404, missing.text


def test_a_manifest_cannot_declare_replicate_on_a_call_that_must_not_cross(rig):
    """
    Four shapes where replicating is always wrong, refused at load rather than discovered in
    production: a read has nothing to carry, a render is already driven at both ends by the
    same source, a forwarded call is replicated by the service behind it, and a public one
    would let anyone with no token make this controller call its peer.
    """
    import pytest

    from trixd.common.errors import PluginRefused
    from trixd.core.plugins import manifest as manifests

    base = {"plugin": "probe", "backend": "backend.Backend"}
    for endpoint, because in (
            ({"path": "x", "methods": ["GET"], "handler": "read", "minimum": "reader",
              "replicate": True}, "reads"),
            ({"path": "y", "methods": ["PATCH"], "handler": "w", "minimum": "manager",
              "forward": True, "replicate": True}, "forwarded"),
            ({"path": "z", "methods": ["PATCH"], "handler": "w", "public": True,
              "replicate": True}, "public"),
            ({"path": "r", "methods": ["PATCH"], "minimum": "manager", "handler": "w",
              "render": {"inputs": ["luna.node"], "serve": "json"},
              "replicate": True}, "a render")):
        with pytest.raises(PluginRefused) as refusal:
            manifests.parse({**base, "endpoints": [endpoint]})
        assert because in str(refusal.value), (because, str(refusal.value))
