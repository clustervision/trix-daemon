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
    assert set(models.REPLICATED) == {"declaration", "principal"}


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

    def refuse(peer, row, may_sign_in=True):
        """
        A peer that is not there: the connection is refused, so nothing came back and the
        socket is the only thing that has been learned about.
        """
        tried.append(peer)
        return outbox.QUEUED, "connection refused", outbox.NO_ANSWER

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
    outbox._send = lambda peer, row, may_sign_in=True: (
        sent.append(peer), (outbox.DONE, "", outbox.ANSWERED))[1]
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

    # An endpoint that takes no body sends a null one, and the envelope must accept that.
    # It did not: `body: dict = None` is not optional to pydantic, so a DELETE was refused
    # 422 and the sender could only read that as terminal. Found on a live pair, by the
    # first real call, because every test until this one sent an empty dict instead.
    null_body = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": "directory", "path": "users/<name>", "method": "DELETE",
        "variables": {"name": "someone"}, "body": None, "origin": "ctrl1"})
    assert null_body.status_code != 422, null_body.text


def _crossing_plugin(rig) -> None:
    """
    A site plugin whose one endpoint declares replicate, takes a node and requires a field.

    Written here rather than borrowed from a shipped plugin: the only shipped one that
    declares replicate is not in the test rig's ENABLED list, and a test of what apply
    accepts should not depend on which plugin happens to carry the flag this release.
    """
    import os

    from trixd.common.settings import current as settings
    from trixd.core.plugins import loader

    site = os.path.join(settings().plugins.site_directory, "crossing")
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "manifest.yaml"), "w", encoding="utf-8") as handle:
        handle.write(
            "plugin: crossing\n"
            "backend: backend\n"
            "description: One endpoint that declares replicate, for testing apply.\n"
            "schemas:\n"
            "  shape:\n"
            "    count:\n"
            "      type: integer\n"
            "      description: A number the handler is given\n"
            "endpoints:\n"
            "  - path: thing/<node>\n"
            "    methods: [PATCH]\n"
            "    token: node\n"
            "    handler: thing\n"
            "    schema: shape\n"
            "    replicate: true\n"
            "    summary: A replicating node endpoint\n")
    with open(os.path.join(site, "backend.py"), "w", encoding="utf-8") as handle:
        handle.write("class Backend:\n"
                     "    def thing(self, node: str, body: dict = None) -> dict:\n"
                     '        return {"node": node, "count": (body or {}).get("count")}\n')
    with open(os.path.join(site, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write("")
    assert loader.load(rig, "crossing").state == "loaded"


def test_apply_refuses_a_body_the_endpoint_does_not_declare(rig, client, headers):
    """
    The route handed the handler whatever the peer sent, unchecked, so a body this build's
    schema would refuse reached the handler and came back a 500 - which the outbox reads as
    the peer having stopped answering, holding everything queued behind it. A refusal names
    the field, and is terminal, so the sender stops instead.

    Both directions of a version-skewed pair: a field of the wrong type, and a required one
    the sender never knew about.
    """
    from trixd.common.constants import API_PREFIX

    _crossing_plugin(rig)
    envelope = {"plugin": "crossing", "path": "thing/<node>", "method": "PATCH",
                "variables": {"node": "node001"}, "origin": "ctrl1"}

    wrong = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                        json={**envelope, "body": {"count": "lots"}, "request_id": "r-type"})
    assert wrong.status_code == 400, wrong.text
    assert "count" in wrong.text, wrong.text

    absent = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                         json={**envelope, "body": None, "request_id": "r-absent"})
    assert absent.status_code == 400, absent.text

    good = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                       json={**envelope, "body": {"count": 3}, "request_id": "r-good"})
    assert good.status_code == 200, good.text
    assert good.json()["result"]["count"] == 3, good.text


def test_applying_a_node_endpoint_leaves_a_trail(rig, client, headers):
    """
    No user token reaches a node endpoint, not even rootus, and this route does - which is
    the whole job, since the peer applies on the node's behalf. The peer and a person share
    the account the pair is configured with, so nothing here can tell them apart; what can
    be done is to record that a node endpoint was reached this way, and that was not done.
    """
    from trixd.common.constants import API_PREFIX
    from trixd.core.security import audit

    _crossing_plugin(rig)
    answer = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": "crossing", "path": "thing/<node>", "method": "PATCH",
        "variables": {"node": "node001"}, "body": {"count": 1},
        "origin": "ctrl1", "request_id": "r-trail"})
    assert answer.status_code == 200, answer.text

    applied = [row for row in audit.read(limit=100) if row["action"] == "replication.apply"]
    assert applied, "nothing in the trail says a node endpoint was reached through apply"
    assert applied[0]["object"] == "node001", applied[0]
    assert "ctrl1" in applied[0]["detail"], applied[0]


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

def test_a_call_on_a_request_path_never_signs_in_at_the_peer(rig, monkeypatch):
    """
    The short timeout is worthless if obtaining the token ignores it. Signing in is a
    request with the sweep's own thirty-second timeout, so an inline attempt that signs in
    holds a thread for half a minute against a dark peer - measured at 30s on a live pair,
    which is the stall the breaker exists to prevent arriving through the back door.

    With nothing held the row waits for the sweep, which can afford to sign in.
    """
    from trixd.core.replication import outbox, sweep

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    signed = []
    monkeypatch.setattr(sweep, "_sign_in",
                        lambda peer, verify: signed.append(peer) or "token")
    # Nothing held for this peer, which is the state after any restart.
    monkeypatch.setattr(sweep, "held_token", lambda peer: "")
    monkeypatch.setattr(outbox, "held_token", lambda peer: "")

    for _ in range(5):
        said = outbox.replicate("prometheus", "hw-rules/<node>", "DELETE",
                                {"node": "node001"}, None, {"retired": []}, "req", "tester")
    assert said["peers"] == {"ctrl2": outbox.QUEUED}, said
    assert signed == [], "the request path signed in at the peer"
    # Having no token is a fact about us, not about the peer, so it must not be read as
    # the peer being down: after a restart every peer starts with nothing held.
    assert not outbox.breaker_open("ctrl2"), "a missing token was blamed on the peer"

    # The sweep may, and does.
    outbox.drain("ctrl2")
    assert signed == ["ctrl2"], "the sweep did not sign in to send what was owed"

def test_the_body_and_the_caller_reach_the_peer(rig, monkeypatch):
    """
    What crosses must be what the handler was given. `_arguments` pops `body` and `caller`
    out of the kwargs it is handed, so reading them from those afterwards gives a null body
    and no origin on every call - and the peer accepts that and acts on it wrongly: labels
    not applied, a force ignored, a bulk write of nothing, a rename refused.

    Every other test here drives the outbox directly, which is exactly how that got
    through. This one goes through the bound operation, the way a request does.
    """
    from pydantic import BaseModel

    from trixd.core.plugins import binder
    from trixd.core.plugins.manifest import Endpoint
    from trixd.core.replication import outbox

    seen = {}
    monkeypatch.setattr(outbox, "replicate",
                        lambda *args, **kw: seen.update(plugin=args[0], path=args[1],
                                                        verb=args[2], variables=args[3],
                                                        body=args[4], origin=args[7]) or {})

    def handler(node=None, body=None, caller=None):
        """
        A backend that records what it was handed, as the real ones are handed it.
        """
        seen["handler_body"] = body
        return {"node": node, "changed": True}

    endpoint = Endpoint(path="hw-rules/<node>", methods=["PATCH"], handler="handler",
                        minimum="manager", replicate=True)

    class _Body(BaseModel):
        """
        A body as FastAPI actually hands it over: a model, not a dict.
        """

        nhc: bool = False

    operation = binder._endpoint_function(handler, endpoint, _Body, "prometheus", "PATCH")

    class _Caller:
        name = "someone"

    # A model, because that is what a real request arrives as: a dict here would skip the
    # conversion every real call goes through.
    operation(node="node001", body=_Body(nhc=True), caller=_Caller())

    assert seen["handler_body"] == {"nhc": True}, "the handler itself lost the body"
    assert seen["body"] == {"nhc": True}, \
        f"the peer would have been sent {seen['body']!r} instead of the real body"
    assert seen["origin"] == "someone", f"origin was {seen['origin']!r}, not the caller"
    assert seen["variables"] == {"node": "node001"}, seen["variables"]

def test_a_peer_away_for_a_long_time_is_never_given_up_on(rig, monkeypatch):
    """
    The fail-stop must not become a wedge. A row that is only waiting for a peer stays
    waiting: escalating it to refused after some number of passes would mean a controller
    rebooted for an hour of patching comes back to a terminal row that holds its target
    for ever, with nothing that clears it. What such a row needs is to be seen, which is
    what the owed route and the health counts are for.
    """
    from trixd.core.replication import outbox

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    monkeypatch.setattr(outbox, "_send",
                        lambda peer, row, may_sign_in=True:
                        (outbox.QUEUED, "away", outbox.NO_ANSWER))
    outbox._failures.clear()
    outbox._open.clear()
    try:
        outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                      {"node": "node001"}, None, "req", "tester")
        for _ in range(30):
            outbox.drain("ctrl2")
        assert outbox.states()["failed"] == 0, "a peer merely being away became terminal"
        assert outbox.states()["pending"] == 1, "the row stopped waiting to be sent"
    finally:
        outbox._failures.clear()
        outbox._open.clear()


def test_a_refusal_holds_what_was_already_queued_behind_it(rig, monkeypatch):
    """
    A refused write holds that node's actions that were already queued behind it, because
    `repair` and `rename` relabel and move a file that is already there - `rename_hw_rules`
    is `repair_hw_rules(node, was=...)` and does nothing when there is no file - so a rename
    behind a refused write would silently do nothing.

    It holds that node and nothing else: another node says nothing about this one, and
    holding it too would make one malformed action an outage for everything owed.
    """
    from trixd.core.replication import outbox

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    sent = []

    def send(peer, row, may_sign_in=True):
        """
        A peer that refuses node001's delete outright and takes everything else.
        """
        if row.method == "DELETE" and "node001" in row.payload:
            return outbox.FAILED, "400: malformed", outbox.ANSWERED
        sent.append(row.payload)
        return outbox.DONE, "", outbox.ANSWERED

    monkeypatch.setattr(outbox, "_send", send)
    # All three are queued before the refusal happens, so node001's repair is one that
    # assumed the delete landed rather than a newer statement of what the node should be.
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                  {"node": "node001"}, None, "req", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/repair", "POST",
                  {"node": "node001"}, None, "req", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                  {"node": "node002"}, None, "req", "tester")

    outbox._failures.clear()
    outbox._open.clear()
    try:
        # One pass: the refusal at the head holds its own node's chain and the rest of what
        # the peer owes still goes. It used to take two - the first pass stopped dead at the
        # refusal and sent nothing, so node002 waited an interval for a refusal that was
        # never anything to do with it.
        result = outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()
    assert result["refused"] == 1, result
    assert result["blocked"] == 1, f"node001's repair was not held: {result}"
    assert result["sent"] == 1, result
    assert len(sent) == 1 and "node002" in sent[0], \
        f"sent {sent}: node001's repair should be held behind its refused write, and " \
        f"node002 should have gone"


def test_the_next_action_for_a_node_clears_what_the_peer_refused(rig, monkeypatch):
    """
    A refusal must not wedge a node. Only a call with the same body replaced a refused row,
    so a refused rename held every later action for that node: a different body made a
    second row, which queued behind the first and waited for a call that never came.

    It clears that node's refusal and no other node's: a refusal naming one node says
    nothing about another, and clearing it would throw away the evidence.
    """
    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    sent = []
    monkeypatch.setattr(outbox, "_send",
                        lambda peer, row, may_sign_in=True: (
                            sent.append(row.payload),
                            (outbox.DONE, "", outbox.ANSWERED))[1])
    refused = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/rename", "POST",
                            {"node": "node001"}, {"was": "node000"}, "req", "tester")
    outbox._finish(refused, outbox.FAILED, "400: malformed")
    elsewhere = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                              {"node": "node002"}, None, "req", "tester")
    outbox._finish(elsewhere, outbox.FAILED, "400: malformed")

    # A different body for the same node: the call that used to queue behind the refusal.
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                  {"node": "node001"}, {"nhc": True}, "req", "tester")

    handle = database.session()
    try:
        # The node part of the target, read off the row rather than through the code
        # under test, so this fails on the behaviour and not on an import.
        held = {(row.method, row.target.split("|")[0], row.state)
                for row in handle.scalars(select(models.Outbox)).all()}
    finally:
        handle.close()
    assert ("POST", "node=node001", outbox.FAILED) not in held, \
        f"the refused rename still holds node001: {held}"
    assert ("DELETE", "node=node002", outbox.FAILED) in held, \
        f"an action for node001 cleared node002's refusal: {held}"

    result = outbox.drain("ctrl2")
    assert result["blocked"] == 0, f"node001 is still held behind the refusal: {result}"
    assert len(sent) == 1 and "node001" in sent[0], \
        f"sent {sent}: node001's patch should have gone once the refusal was cleared"


def test_a_forgotten_declaration_does_not_come_back_from_the_peer(rig, tmp_path):
    """
    The defect this closes is only visible across two controllers: deleting the row outright
    leaves the peer holding it, and the next sweep puts it back - within thirty seconds, with
    nothing said. A marked row carries the deletion instead, and counts as no row on both.
    """
    from trixd.core.replication import tables
    from trixd.db import engine as database
    from trixd.db import models
    from trixd.utils import declarations

    declarations.write("prometheus", "exporter-scope", "node:node001", {"ipmi": False})
    handle = database.session()
    peer = _peer(tmp_path)
    try:
        tables.merge_table(peer, "declaration", tables.export_table(handle, "declaration"))
        assert peer.scalar(select(models.Declaration).where(
            models.Declaration.name == "node:node001")) is not None, "the peer never had it"

        assert declarations.forget("prometheus", "exporter-scope", "node:node001") is True
        handle.expire_all()
        # A sweep in each direction, as the pair does every thirty seconds. The peer's own
        # document goes first, while it still holds the live row: the other order overwrites
        # the peer before the reverse sweep runs, and then nothing is being tested.
        tables.merge_table(handle, "declaration", tables.export_table(peer, "declaration"))
        tables.merge_table(peer, "declaration", tables.export_table(handle, "declaration"))

        theirs = peer.scalar(select(models.Declaration).where(
            models.Declaration.name == "node:node001"))
        assert theirs.deleted, "the deletion did not reach the peer"
        assert declarations.read("prometheus", "exporter-scope", "node:node001") is None, \
            "the row came back as a document here"
        assert "node:node001" not in [row["name"] for row
                                      in declarations.listing("prometheus", "exporter-scope")], \
            "a forgotten declaration is still listed"
    finally:
        handle.close()
        peer.close()


def test_a_peer_cannot_name_a_role_this_daemon_does_not_grant(rig):
    """
    The API refuses a role off the ladder, and the ladder leaves out rootus deliberately -
    that is the ini account, not a role anyone is given. The merge took the peer's string as
    it came, so a compromised or impersonated peer could write rootus into a principal row
    and outrank manager on every route, in a value no API here would show as invalid.
    """
    handle = database.session()
    try:
        tables.merge_table(handle, "principal", [
            {"key": ["climber"], "role": "rootus", "password_digest": "", "enabled": True,
             "version": 2, "origin": "peer", "updated_at": "2026-10-07 10:00:00"},
            {"key": ["gibberish"], "role": "king", "password_digest": "", "enabled": True,
             "version": 2, "origin": "peer", "updated_at": "2026-10-07 10:00:00"},
            {"key": ["ordinary"], "role": "manager", "password_digest": "", "enabled": True,
             "version": 2, "origin": "peer", "updated_at": "2026-10-07 10:00:00"}])
        held = {row.name: row.role for row in handle.scalars(
            select(models.Principal).where(
                models.Principal.name.in_(["climber", "gibberish", "ordinary"]))).all()}
    finally:
        handle.close()

    assert held["climber"] == "none", "a peer wrote rootus into a principal row"
    assert held["gibberish"] == "none", "a peer wrote a role off the ladder"
    assert held["ordinary"] == "manager", "a role this daemon does grant was not kept"


def test_forgetting_a_declaration_that_is_already_a_marker_still_moves_it_on(rig):
    """
    The marker can be newer here than at a peer that still holds the row live, and the pair
    then sits at equal versions with different content, which no sweep breaks. Issuing the
    delete again is the way out, so the second call bumps rather than answering nothing.
    """
    from trixd.utils import declarations

    declarations.write("prometheus", "exporter-scope", "node:node003", {"ipmi": False})
    assert declarations.forget("prometheus", "exporter-scope", "node:node003") is True
    handle = database.session()
    try:
        marked = handle.scalar(select(models.Declaration).where(
            models.Declaration.name == "node:node003"))
        first = marked.version

        assert declarations.forget("prometheus", "exporter-scope", "node:node003") is False

        handle.expire_all()
        marked = handle.scalar(select(models.Declaration).where(
            models.Declaration.name == "node:node003"))
        assert marked.deleted, "the row stopped being a marker"
        assert marked.version > first, \
            "a delete issued again left the version where it was, so the tie cannot be broken"
    finally:
        handle.close()


def test_writing_a_forgotten_key_again_brings_it_back(rig):
    """
    A marker must not be a tombstone nobody can get past: the same name written again is a
    live document, or a scope could never be set twice.
    """
    from trixd.utils import declarations

    declarations.write("prometheus", "exporter-scope", "group:gpu", {"lshw": False})
    declarations.forget("prometheus", "exporter-scope", "group:gpu")
    assert declarations.read("prometheus", "exporter-scope", "group:gpu") is None

    declarations.write("prometheus", "exporter-scope", "group:gpu", {"nvidia": True})

    assert declarations.read("prometheus", "exporter-scope", "group:gpu") == {"nvidia": True}
    assert "group:gpu" in [row["name"] for row
                           in declarations.listing("prometheus", "exporter-scope")]


def test_a_hard_delete_comes_back_from_the_peer(rig, tmp_path):
    """
    Why the marker exists, pinned so the reason cannot quietly stop being true.

    If this ever fails, the sweep no longer re-adds a row that is missing here, and forgetting
    could become an ordinary delete.
    """
    from trixd.core.replication import tables
    from trixd.db import engine as database
    from trixd.db import models
    from trixd.utils import declarations

    declarations.write("prometheus", "exporter-scope", "node:node009", {"ipmi": False})
    handle = database.session()
    peer = _peer(tmp_path)
    try:
        tables.merge_table(peer, "declaration", tables.export_table(handle, "declaration"))
        handle.execute(models.Declaration.__table__.delete().where(
            models.Declaration.name == "node:node009"))
        handle.commit()
        assert declarations.read("prometheus", "exporter-scope", "node:node009") is None

        tables.merge_table(handle, "declaration", tables.export_table(peer, "declaration"))

        assert declarations.read("prometheus", "exporter-scope",
                                 "node:node009") == {"ipmi": False}, \
            "a row deleted outright no longer returns, so the marker may not be needed"
    finally:
        handle.close()
        peer.close()


def test_every_mapped_table_is_classified_as_replicated_or_local(rig):
    """
    A table in neither list does not replicate, and nothing says so: the sweep iterates
    the list, so the pass reports clean while the table never crosses. The outbox was
    such a table, and this is the list that learns about the next one for free.
    """
    from trixd.db import models

    mapped = {mapper.class_.__tablename__ for mapper in models.Base.registry.mappers}
    classified = set(models.REPLICATED) | set(models.LOCAL)
    assert mapped == classified, sorted(mapped ^ classified)


def test_an_exact_tie_resolves_the_same_way_on_both_controllers(rig, tmp_path):
    """
    Same version and same timestamp with different content: neither row is newer, so each
    controller kept its own and the two copies then stayed different for ever, with a
    warning on both sides and no pass able to resolve it. The origin decides instead,
    which both sides read the same way.
    """
    from datetime import datetime

    peer = _peer(tmp_path)
    stamped = datetime(2026, 10, 17, 12, 0, 0)
    handle = database.session()
    try:
        handle.add(models.Principal(name="tied", role="operator", origin="ctrl1",
                                    version=4, updated_at=stamped))
        peer.add(models.Principal(name="tied", role="manager", origin="ctrl2",
                                  version=4, updated_at=stamped))
        handle.commit()
        peer.commit()

        ours = tables.merge_table(handle, "principal", tables.export_table(peer, "principal"))
        tables.merge_table(peer, "principal", tables.export_table(handle, "principal"))

        assert ours["conflicts"] == 1, ours
        assert (tables.table_hash(handle, "principal")
                == tables.table_hash(peer, "principal")), "the tie left the pair diverged"
        held = handle.scalar(select(models.Principal).where(models.Principal.name == "tied"))
        assert held.role == "manager" and held.origin == "ctrl2"
    finally:
        handle.close()
        peer.close()


def test_exactly_one_side_wins_a_tie(rig):
    """
    The property that makes it converge: run over the same pair from either controller,
    the answer names the same document. Keeping the local row satisfies neither side.
    """
    from trixd.core.replication.tables import _wins_tie

    one = {"key": ["tied"], "origin": "ctrl1", "role": "operator"}
    two = {"key": ["tied"], "origin": "ctrl2", "role": "manager"}
    assert _wins_tie("principal", two, one) != _wins_tie("principal", one, two)

    # Same origin as well: a digest still separates them, so there is always an answer.
    same_one = {"key": ["tied"], "origin": "ctrl1", "role": "operator"}
    same_two = {"key": ["tied"], "origin": "ctrl1", "role": "manager"}
    assert (_wins_tie("principal", same_two, same_one)
            != _wins_tie("principal", same_one, same_two))


def test_a_pass_that_cannot_converge_is_said_and_counted(rig):
    """
    Taking everything the peer had that was newer is not the same as agreeing with it.
    A pass that still differed afterwards reported success and said nothing, so the only
    sign was the whole table being fetched again every interval, for ever.
    """
    from trixd.core.replication.sweep import STUBBORN, Sweep

    walking = Sweep()
    both = ["principal", "declaration"]
    theirs = {"principal": "aaa", "declaration": "same"}
    ours = {"principal": "bbb", "declaration": "same"}

    for _ in range(STUBBORN - 1):
        walking._converged("peer1", both, ["principal"], theirs, ours)
        assert walking.stalled() == 0, "one differing pass is ordinary and stays quiet"
    walking._converged("peer1", both, ["principal"], theirs, ours)
    assert walking.stalled() == 1, "a table differing pass after pass was never reported"

    # And it stops being said once the pair agrees, without a restart.
    walking._converged("peer1", both, [], theirs, theirs)
    assert walking.stalled() == 0


def test_health_says_how_many_tables_are_not_converging(client):
    """
    The counts are what a monitor reads; a pair that cannot converge was in none of them.
    """
    answer = client.get(f"{API_PREFIX}/system/health")
    assert answer.status_code == 200
    reported = answer.json()["triggers"]["replication"]
    assert "not_converging" in reported, reported
    assert reported["not_converging"] == 0


def test_one_row_the_peer_answers_badly_for_does_not_hold_the_others(rig, monkeypatch):
    """
    A 5xx is the peer answering, so it says nothing about the next node - and stopping the
    pass there held everything else owed to that peer, pass after pass, with nothing able
    to clear it. It holds its own node's chain and no more.
    """
    import json as jsonlib

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    for node in ("node001", "node002", "node003"):
        outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                      {"node": node}, {"force": True}, f"req-{node}", "tester")

    def answer(peer, row, may_sign_in=True):
        """The peer is up, and one node's row goes wrong on it."""
        if jsonlib.loads(row.payload)["variables"]["node"] == "node001":
            return outbox.QUEUED, "500: it went wrong here", outbox.ANSWERED
        return outbox.DONE, "", outbox.ANSWERED

    monkeypatch.setattr(outbox, "_send", answer)
    outbox._failures.clear()
    outbox._open.clear()
    try:
        result = outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()

    # The states, not the counts: drain orders by creation and the counts come out the same
    # whichever row went wrong, so a pass that stopped dead at a late row would have read
    # identically. What the states cannot be mistaken about is which rows the peer took.
    handle = database.session()
    try:
        rows = {jsonlib.loads(row.payload)["variables"]["node"]: row
                for row in handle.scalars(select(models.Outbox)).all()}
        assert rows["node001"].state == outbox.QUEUED, rows["node001"].state
        assert "500" in rows["node001"].message, rows["node001"].message
        assert rows["node002"].state == outbox.DONE, rows["node002"].state
        assert rows["node003"].state == outbox.DONE, rows["node003"].state
    finally:
        handle.close()
    assert result["sent"] == 2, result


def test_a_peer_that_has_stopped_answering_stops_the_pass_at_one_attempt(rig, monkeypatch):
    """
    The other half of the same branch, and the half that costs real time: nothing came back,
    so the rest of the queue is not offered. Each attempt is a three-second wait against a
    peer that is not there, so what this pins is the *number of attempts* - the counts come
    out the same whether the pass stopped or held every row, which is how the first version
    of this test passed while the branch it named could not be reached at all.
    """
    from trixd.core.replication import outbox

    for node in ("node001", "node002", "node003"):
        outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                      {"node": node}, {"force": True}, f"dark-{node}", "tester")

    tried = []

    def silence(peer, row, may_sign_in=True):
        """A peer that is not there: the socket says nothing back."""
        tried.append(row.path)
        return outbox.QUEUED, "connection refused", outbox.NO_ANSWER

    monkeypatch.setattr(outbox, "_send", silence)
    outbox._failures.clear()
    outbox._open.clear()
    try:
        result = outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()
    assert len(tried) == 1, f"a dark peer was offered {len(tried)} rows, not one"
    assert result["sent"] == 0, result
    assert result["left"] == 3, result


def test_a_peer_saying_it_skipped_is_not_recorded_as_done(rig, monkeypatch):
    """
    The peer answers 200 and its body says it did not do the work - the node is not in its
    own Luna. That was read as done and pruned an hour later, so the only account of the
    peer being behind for that node was gone.
    """
    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    class Answer:
        """What the apply route answers when its handler declined to act."""
        status_code = 200
        text = ""

        def json(self):
            return {"applied": "prometheus hw-rules/<node>", "origin": "ctrl1",
                    "result": {"node": "node001", "skipped": "not in Luna"}}

    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                  {"node": "node001"}, {"force": True}, "req-skip", "tester")
    monkeypatch.setattr(outbox.requests, "post", lambda *args, **kwargs: Answer())
    monkeypatch.setattr(outbox, "trust", lambda: True)
    monkeypatch.setattr(outbox, "peer_token", lambda peer, verify, renew=False: "a-token")

    result = outbox.drain("ctrl2")
    assert result["sent"] == 0, result

    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox))
        assert row is not None, "the row was pruned, so nothing says the peer is behind"
        assert row.state == outbox.QUEUED, row.state
        assert "not in Luna" in row.message, row.message
    finally:
        handle.close()


def test_a_refusal_nothing_clears_is_let_go_of_with_what_it_held(rig, monkeypatch):
    """
    A refused row holds its node's later actions until somebody acts on that node again.
    Where nobody does, that hold was permanent and lived in a row nothing reaped. What was
    held goes with it rather than being sent: those actions assume it landed.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    stuck = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                          {"node": "node001"}, {"force": True}, "req-stuck", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/rename", "POST",
                  {"node": "node001"}, {"was": "node000"}, "req-behind", "tester")
    # Another node's row, which the refusal was never anything to do with.
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                  {"node": "node002"}, {"force": True}, "req-other", "tester")

    handle = database.session()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == stuck))
        row.state = outbox.FAILED
        row.message = "400: this build serves no such endpoint"
        # Refused a day ago, and what it holds was queued before that - which is the only
        # way a row can be behind it: submitting one afterwards clears the refusal.
        row.finished = now - timedelta(seconds=outbox.KEEP_REFUSED + 60)
        for other in handle.scalars(select(models.Outbox)).all():
            other.created = now - timedelta(seconds=outbox.KEEP_REFUSED + 120)
        handle.commit()
    finally:
        handle.close()

    # Through drain, not by calling the reap: with the call removed from drain the whole
    # suite stayed green, so the reap would never have run on a controller.
    monkeypatch.setattr(outbox, "_send", lambda peer, row, may_sign_in=True:
                        (outbox.DONE, "", outbox.ANSWERED))
    outbox._failures.clear()
    outbox._open.clear()
    try:
        outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()

    handle = database.session()
    try:
        left = handle.scalars(select(models.Outbox)).all()
        assert [row.target.split("=")[1].split("|")[0] for row in left] == ["node002"], \
            [(row.state, row.target) for row in left]
        assert left[0].state == outbox.DONE, left[0].state
    finally:
        handle.close()


def test_a_fresh_refusal_is_kept_as_evidence(rig):
    """
    The reap is for a refusal nothing has cleared in a day, not for one somebody may still
    be reading in health or in owed.
    """
    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    stuck = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                          {"node": "node001"}, {"force": True}, "req-new", "tester")
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == stuck))
        row.state = outbox.FAILED
        row.finished = datetime_now()
        handle.commit()
    finally:
        handle.close()

    assert outbox._reap("ctrl2") == 0
    handle = database.session()
    try:
        assert handle.scalar(select(models.Outbox)) is not None
    finally:
        handle.close()


def datetime_now():
    """
    Now, as the outbox stores it: naive UTC.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_the_same_call_offered_twice_is_applied_once(rig, client, headers):
    """
    Delivery is at-least-once: the sender gives the apply three seconds and offers the row
    again every pass until it hears otherwise. The call was replayed rather than the answer,
    so a peer slower than that had the write applied again at every pass - and the second
    answer came back saying it changed nothing, which the sender cannot tell from the peer
    declining to do the work.
    """
    from trixd.common.constants import API_PREFIX
    from trixd.core.plugins.registry import current as registry
    from trixd.core.replication import applied

    applied.forget_all()
    _crossing_plugin(rig)
    entry = registry().plugins["crossing"]
    ran = []
    standing = entry.backend.thing

    def counting(node: str, body: dict = None) -> dict:
        """The same handler, counting how many times the peer's call reached it."""
        ran.append(node)
        return standing(node, body)

    entry.backend.thing = counting
    envelope = {"plugin": "crossing", "path": "thing/<node>", "method": "PATCH",
                "variables": {"node": "node001"}, "body": {"count": 3},
                "origin": "ctrl1", "request_id": "r-repeat"}
    try:
        first = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                            json=envelope)
        again = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                            json=envelope)
    finally:
        entry.backend.thing = standing
        applied.forget_all()

    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert ran == ["node001"], f"the call ran {len(ran)} times"
    assert again.json() == first.json(), "the repeat answered something else"


def test_a_difference_that_is_moving_is_not_called_unconverged(rig):
    """
    A table differing at the end of a pass is ordinary: our row may simply be the newer one,
    and the peer takes it on its own next pass. Counting passes alone therefore called a
    healthy pair with a trickle of local writes unconverged after three of them, and said so
    as an error. The count only grows while neither side has moved.
    """
    from trixd.core.replication.sweep import STUBBORN, Sweep

    walking = Sweep()
    for step in range(STUBBORN + 2):
        # A local write every pass, so our hash is never the one it was.
        walking._converged("peer1", ["principal"], ["principal"],
                           {"principal": "theirs"}, {"principal": f"ours-{step}"})
    assert walking.stalled() == 0, "a pair that is still moving was called unconverged"


def test_a_peer_that_answers_no_hashes_does_not_clear_the_count(rig):
    """
    Both attempts refused leaves no hashes to compare, which is not the same as nothing
    differing - and it cleared the count for exactly the table nobody could look at, so
    health read zero while the divergence stood.
    """
    from trixd.core.replication.sweep import STUBBORN, Sweep

    walking = Sweep()
    theirs, ours = {"principal": "aaa"}, {"principal": "bbb"}
    for _ in range(STUBBORN):
        walking._converged("peer1", ["principal"], ["principal"], theirs, ours)
    assert walking.stalled() == 1

    # A pass that could compare nothing: no table is comparable, so none is judged.
    walking._converged("peer1", [], [], {}, ours)
    assert walking.stalled() == 1, "the count went when the hashes could not be compared"


def test_a_tie_that_taking_would_not_settle_keeps_the_local_row(rig, tmp_path):
    """
    The merge writes columns, not the document: a role this daemon would not grant is
    written as none, so the row would still differ from the document at the same version
    and time - a tie remade on every pass, with the same write and the same claim each time.
    """
    from datetime import datetime

    peer = _peer(tmp_path)
    stamped = datetime(2026, 10, 17, 12, 0, 0)
    handle = database.session()
    try:
        handle.add(models.Principal(name="odd", role="operator", origin="ctrl1",
                                    version=4, updated_at=stamped))
        # rootus is the ini account, not a role on the ladder; the API refuses it, and the
        # merge writes it as none - so taking this document cannot settle the tie.
        peer.add(models.Principal(name="odd", role="rootus", origin="ctrl2",
                                  version=4, updated_at=stamped))
        handle.commit()
        peer.commit()

        # Both ways, as a pass on each controller does: a veto that only looked at the
        # incoming document left the side holding the unsound row keeping it, so the tie
        # resolved differently on the two and the pair stayed apart - and this test asserted
        # only that the local row survived, which was true on one of them.
        tables.merge_table(handle, "principal", tables.export_table(peer, "principal"))
        tables.merge_table(peer, "principal", tables.export_table(handle, "principal"))

        held = handle.scalar(select(models.Principal).where(models.Principal.name == "odd"))
        theirs = peer.scalar(select(models.Principal).where(models.Principal.name == "odd"))
        assert held.role == "operator", "a role off the ladder arrived through the tie"
        assert theirs.role == "operator", \
            f"the peer kept {theirs.role!r}: the unsound row has to lose on both sides"
        assert (tables.table_hash(handle, "principal")
                == tables.table_hash(peer, "principal")), "the pair stayed apart"
    finally:
        handle.close()
        peer.close()


def test_a_decline_is_not_asked_for_ever(rig, monkeypatch):
    """
    A queued row is cleared by nothing: the next action for a node clears only a refusal,
    and the reap collects only a refusal. So a decline rode the sweep for ever and held its
    node's later actions with it - and the new action that was meant to clear it was held
    too. Declining for long enough makes it terminal, which is what gives it both exits.

    A span, not a count of attempts: `attempts` counts every outcome, so eleven passes
    against a peer being patched plus one decline used to be enough, and a decline is often
    a moment of Luna lag that the next pass clears. So both halves are asserted here - a
    fresh row that is declined once stays queued, and one declining for longer than it is
    asked for goes terminal.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    class Answer:
        """What the apply route answers when its handler declined to act."""
        status_code = 200
        text = ""

        def json(self):
            return {"applied": "prometheus hw-rules/<node>", "origin": "ctrl1",
                    "result": {"node": "node001", "skipped": "not in Luna"}}

    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                           {"node": "node001"}, {"force": True}, "req-decline", "tester")
    monkeypatch.setattr(outbox.requests, "post", lambda *args, **kwargs: Answer())
    monkeypatch.setattr(outbox, "trust", lambda: True)
    monkeypatch.setattr(outbox, "peer_token", lambda peer, verify, renew=False: "a-token")

    # Driven through the real send, so the message the decline is recognised by is the one
    # the send produces: the two are a pair, and a test that built the message itself would
    # pass while a reworded one broke the escalation.
    outbox.drain("ctrl2")
    handle = database.session()
    try:
        young = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        assert young.state == outbox.QUEUED, \
            f"one decline on a fresh row made it {young.state}"
        young.created = (datetime_now()
                         - timedelta(seconds=outbox.DECLINED_FOR_SECONDS + 60))
        handle.commit()
    finally:
        handle.close()
    outbox.drain("ctrl2")

    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        assert row.state == outbox.FAILED, \
            f"after {row.attempts} declines the row is still {row.state}, so nothing " \
            f"will ever clear it"
    finally:
        handle.close()


def test_a_declined_apply_is_not_remembered(rig, client, headers):
    """
    A decline is offered again, and the row carries the same request id every pass - so
    remembering the answer would hand every retry the decline for as long as the record
    lives, and the peer would stay behind for that node.
    """
    from trixd.common.constants import API_PREFIX
    from trixd.core.plugins.registry import current as registry
    from trixd.core.replication import applied

    applied.forget_all()
    _crossing_plugin(rig)
    entry = registry().plugins["crossing"]
    ran = []

    def declining(node: str, body: dict = None) -> dict:
        """A handler that cannot act: the node is not in this controller's Luna."""
        ran.append(node)
        return {"node": node, "skipped": "not in Luna"}

    standing = entry.backend.thing
    entry.backend.thing = declining
    envelope = {"plugin": "crossing", "path": "thing/<node>", "method": "PATCH",
                "variables": {"node": "node001"}, "body": {"count": 1},
                "origin": "ctrl1", "request_id": "r-declined"}
    try:
        first = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                            json=envelope)
        again = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                            json=envelope)
    finally:
        entry.backend.thing = standing
        applied.forget_all()

    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert ran == ["node001", "node001"], \
        f"the handler ran {len(ran)} times: a decline must be offered again, not answered " \
        f"from the record"


def test_two_senders_do_not_deliver_one_row_twice(rig, monkeypatch):
    """
    Both senders live in this process - the sweep's drain and a caller's inline attempt -
    and both read the row as queued before either marks it, so the same call went to the
    peer twice and only the peer's own record stood between that and the write happening
    twice.
    """
    from trixd.core.replication import outbox

    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                           {"node": "node001"}, {"force": True}, "req-race", "tester")
    tried = []
    monkeypatch.setattr(outbox, "_send",
                        lambda peer, row, may_sign_in=True: (tried.append(row.path),
                                                             (outbox.DONE, "",
                                                              outbox.ANSWERED))[1])

    assert outbox._claim(row_id), "nothing held the row yet"
    try:
        # The other sender, arriving while the first is still in flight.
        outbox._attempt("ctrl2", row_id)
    finally:
        outbox._release(row_id)
    assert tried == [], "the row was offered while the other sender had it in flight"

    # Once it is given back, the row is sent as usual.
    outbox._attempt("ctrl2", row_id)
    assert len(tried) == 1, tried


def test_a_pair_that_cannot_converge_reaches_health(rig, client, monkeypatch):
    """
    The count has to be reached through a pass, not by calling the counter: with both of
    `pass_over`'s calls to it stubbed out the whole suite stayed green, which would have
    left `not_converging` at zero on a controller for ever with nothing to say so.
    """
    from trixd.common.constants import API_PREFIX
    from trixd.core.replication.sweep import STUBBORN
    from trixd.core.replication.sweep import current as sweep

    walking = sweep()
    walking.stubborn.clear()

    def answers(peer, path):
        """A peer holding other tables that never takes ours, and offering no documents."""
        if path == "hashes":
            return {"hashes": {table: f"theirs-{table}" for table in tables.ORDER}}
        return {"documents": []}

    monkeypatch.setattr(walking, "_get", answers)
    try:
        for _ in range(STUBBORN):
            last = walking.pass_over("ctrl2")
        # The passes really did end differing - without this the health value below would
        # be zero for the ordinary reason rather than the one under test.
        assert last["unconverged"] == list(tables.ORDER), last

        reported = client.get(f"{API_PREFIX}/system/health").json()["triggers"]["replication"]
        assert reported["not_converging"] == len(tables.ORDER), reported
    finally:
        walking.stubborn.clear()


def test_health_says_how_long_the_oldest_owed_call_has_waited(rig):
    """
    The counts cannot tell a queue that is busy from one that is stuck; how long the oldest
    has waited can, and a monitor reads it.
    """
    from datetime import timedelta

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    assert outbox.states()["oldest_pending"] == 0, "nothing is owed yet"

    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                           {"node": "node001"}, {"force": True}, "req-age", "tester")
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        row.created = datetime_now() - timedelta(seconds=600)
        handle.commit()
    finally:
        handle.close()

    waited = outbox.states()["oldest_pending"]
    assert 580 <= waited <= 660, waited


def test_the_reap_leaves_an_intent_newer_than_the_refusal_it_lets_go_of(rig):
    """
    The reap drops what a refusal was holding, because those actions assume it landed. A row
    submitted *after* the refusal was written down is not one of them - submitting it cleared
    the refusal, so it is the newer statement of what the node should be - and deleting it
    would be the reap throwing away an intent nobody has sent yet.

    The other reap test ages every row behind the refusal, so this case never arises there.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    stuck = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                          {"node": "node001"}, {"force": True}, "req-stuck", "tester")
    later = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/repair", "POST",
                          {"node": "node001"}, None, "req-later", "tester")

    handle = database.session()
    try:
        now = datetime_now()
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == stuck))
        row.state = outbox.FAILED
        row.message = "400: this build serves no such endpoint"
        row.finished = now - timedelta(seconds=outbox.KEEP_REFUSED + 60)
        # Written down after the refusal was, which is the whole point.
        after = handle.scalar(select(models.Outbox).where(models.Outbox.id == later))
        after.created = now
        handle.commit()
    finally:
        handle.close()

    assert outbox._reap("ctrl2") == 1, "the newer intent was let go of with the refusal"

    handle = database.session()
    try:
        left = handle.scalars(select(models.Outbox)).all()
        assert [row.id for row in left] == [later], [(r.id, r.state) for r in left]
    finally:
        handle.close()


def test_a_pass_reports_what_is_still_owed_after_it(rig, monkeypatch):
    """
    `left` is what the peer still owes once the pass is over, so it is counted after the
    reap rather than before: counted before, it included the rows the same pass had just
    let go of, and a caller reading POST /system/replication/sweep was told the peer owed
    actions that no longer existed.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    stuck = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                          {"node": "node001"}, {"force": True}, "req-old", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                  {"node": "node002"}, {"force": True}, "req-other", "tester")

    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == stuck))
        row.state = outbox.FAILED
        row.finished = datetime_now() - timedelta(seconds=outbox.KEEP_REFUSED + 60)
        handle.commit()
    finally:
        handle.close()

    monkeypatch.setattr(outbox, "_send", lambda peer, row, may_sign_in=True:
                        (outbox.DONE, "", outbox.ANSWERED))
    outbox._failures.clear()
    outbox._open.clear()
    try:
        result = outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()

    handle = database.session()
    try:
        owed = handle.scalars(select(models.Outbox).where(
            models.Outbox.state.in_((outbox.QUEUED, outbox.FAILED)))).all()
    finally:
        handle.close()
    assert result["left"] == len(owed), \
        f"the pass says {result['left']} still owed and the table holds {len(owed)}"


def test_a_tie_between_documents_of_different_shape_keeps_the_local_row(rig, tmp_path):
    """
    Part way through an upgrade one build's document carries a field the other's does not.
    At an exact tie nothing can settle that: taking the shorter document writes the field it
    lacks back to its default, and the build without the field cannot write it at all, so no
    choice converges. The local row stands and the pair is left visibly apart - which the
    sweep counts and health reports as not_converging.

    Pinned because it is the one branch of the tie a shipped build cannot reach, so nothing
    else here would notice it going.
    """
    from datetime import datetime

    stamped = datetime(2026, 10, 17, 12, 0, 0)
    handle = database.session()
    try:
        handle.add(models.Principal(name="shaped", role="operator", origin="ctrl1",
                                    version=4, updated_at=stamped))
        handle.commit()

        mine = tables.export_table(handle, "principal")[0]
        # What the other build sends: the same row, and a field this one has never seen.
        incoming = {**mine, "role": "manager", "origin": "ctrl2", "nickname": "extra"}
        outcome = tables.merge_table(handle, "principal", [incoming])

        held = handle.scalar(select(models.Principal).where(
            models.Principal.name == "shaped"))
        assert outcome["conflicts"] == 1, outcome
        assert outcome["updated"] == 0, outcome
        assert held.role == "operator", \
            f"took a document of a different shape: role is now {held.role!r}"
        assert held.origin == "ctrl1", held.origin
    finally:
        handle.close()
