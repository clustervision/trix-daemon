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

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from trixd.common.constants import API_PREFIX, NODE_PATH_PARAMS
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


def _sweep_said(peers: list, enabled: bool, monkeypatch) -> str:
    """
    What start() says about a replication section, and whether it started.

    Through a handler on the sweep's own logger at its own level: the daemon's loggers
    do not propagate once they are configured, so pytest's capture may never see them.
    """
    import logging as logmodule

    from trixd.common import settings as configuration
    from trixd.core.replication import sweep as replication

    config = configuration.Settings()
    config.replication = configuration.ReplicationSettings(peers=peers, enabled=enabled)
    monkeypatch.setattr(replication, "settings", lambda: config)
    records = []
    handler = logmodule.Handler()
    handler.emit = records.append
    logger = replication.LOGGER
    previous = logger.level
    logger.setLevel(logmodule.DEBUG)
    logger.addHandler(handler)
    try:
        assert replication.current().start() is False, "neither state may start a sweep"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return "\n".join(record.getMessage() for record in records)


def test_a_pair_with_the_switch_off_is_not_told_it_has_no_peers(monkeypatch):
    """
    A configured pair with ENABLED = no is the state someone greps this line for, and
    being told the peers are not configured is the opposite of true: the peers are the
    only reason the switch matters at all. Each state says which of the two it is.
    """
    said = _sweep_said(["peer.cluster:8050"], False, monkeypatch)
    # The precondition: the line was said at all, so the rest is not measuring silence.
    assert "no sweep is running" in said, "the sweep stopped without saying so"
    assert "no replication peers configured" not in said, \
        "a configured pair was told it has no peers, which is the opposite of true"
    assert "ENABLED" in said, "the switch that actually stopped the sweep was not named"
    assert "peer.cluster:8050" in said, "the peers left unreconciled were not named"


def test_a_controller_with_no_peers_still_says_exactly_that(monkeypatch):
    """
    The other half of the split: with nothing configured the original wording stands,
    because that is the state it describes and it is what a site greps for.
    """
    said = _sweep_said([], True, monkeypatch)
    assert "no replication peers configured, so no sweep is running" in said
    assert "ENABLED" not in said, "nothing is held up by the switch when there are no peers"


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

    # A plugin this controller does not have is not a malformed action, so it is refused
    # retryably rather than terminally - see the two tests below this one.
    missing = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": "nosuch", "path": "x", "method": "PATCH", "origin": "ctrl1"})
    assert missing.status_code >= 500, missing.text

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


def _what_the_sender_does_with(status: int, monkeypatch) -> str:
    """
    The state the outbox puts a row in when the peer answers this status, asked of the
    outbox rather than read off the number here.
    """
    from trixd.core.replication import outbox

    class Answer:
        """One refusal from the peer, in the shape requests hands back."""

        status_code = status
        text = '{"message": "a refusal"}'

        def json(self):
            """The body the error handler writes."""
            return {"message": "a refusal"}

    class Row:
        """The fields sending reads off an outbox row."""

        plugin, path, method = "crossing", "thing/<node>", "PATCH"
        payload = '{"variables": {"node": "node001"}, "body": {"count": 1}}'
        origin, request_id = "ctrl1", "r-class"

    monkeypatch.setattr(outbox.requests, "post", lambda *args, **kwargs: Answer())
    monkeypatch.setattr(outbox, "trust", lambda: True)
    monkeypatch.setattr(outbox, "peer_token", lambda peer, verify, renew=False: "a-token")
    return outbox._send("ctrl2", Row())[0]


def test_a_plugin_unplugged_on_the_peer_keeps_the_action_instead_of_dropping_it(
        rig, client, headers, monkeypatch):
    """
    A plugin unplugged on one controller is temporary, and plugging it back in is what makes
    the action apply. Answered as "no such thing", the sender made the row terminal on its
    first attempt and discarded it - and the state these endpoints carry is exactly the state
    no replicated table can repair, so the pair stayed apart with nothing able to heal it.

    Asserted through the sender's own classification, because the number is not the point:
    what matters is which class the outbox puts the answer in.
    """
    from trixd.common.constants import API_PREFIX
    from trixd.core.replication import outbox

    _crossing_plugin(rig)
    unplugged = client.post(f"{API_PREFIX}/system/plugins/crossing/disable", headers=headers)
    assert unplugged.status_code == 200, unplugged.text
    envelope = {"plugin": "crossing", "path": "thing/<node>", "method": "PATCH",
                "variables": {"node": "node001"}, "body": {"count": 1},
                "origin": "ctrl1", "request_id": "r-unplugged"}

    refused = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers,
                          json=envelope)

    assert refused.status_code >= 400, \
        f"the plugin is unplugged here, so this call cannot have applied: {refused.text}"
    assert _what_the_sender_does_with(refused.status_code, monkeypatch) == outbox.QUEUED, \
        f"the peer answered {refused.status_code} for a plugin it has unplugged, which the " \
        f"sender treats as {_what_the_sender_does_with(refused.status_code, monkeypatch)}: " \
        f"the row is discarded on its first attempt and plugging the plugin back in " \
        f"replays nothing"


def test_an_endpoint_this_build_does_not_serve_is_still_terminal(
        rig, client, headers, monkeypatch):
    """
    The other side of the same line, so a fix for the one above cannot quietly make every
    refusal retryable: a peer naming an endpoint this build does not have is a version skew,
    and no number of passes changes it.
    """
    from trixd.common.constants import API_PREFIX
    from trixd.core.replication import outbox

    _crossing_plugin(rig)

    refused = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": "crossing", "path": "thing/<gone>", "method": "PATCH",
        "variables": {"gone": "node001"}, "body": None, "origin": "ctrl1",
        "request_id": "r-skew"})

    assert refused.status_code >= 400, refused.text
    assert _what_the_sender_does_with(refused.status_code, monkeypatch) == outbox.FAILED, \
        f"an endpoint this build does not serve was answered {refused.status_code}, which " \
        f"the sender keeps offering: it rides the sweep for an hour over a skew that no " \
        f"pass can settle"


def _apply_route_refusals() -> set:
    """
    Every error class the apply route raises, read out of the route itself.

    A refusal added later has to be classified by the test below or it fails: one refusal
    sitting in the wrong class is what this whole pair of tests exists for, and a list of
    the refusals somebody once knew about is how the next one gets in.
    """
    import ast
    import inspect

    from trixd.api.v1 import system

    tree = ast.parse(inspect.getsource(system.replication_apply))
    return {node.exc.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)}


def test_every_refusal_the_apply_route_raises_is_in_a_class_the_sender_was_told_about(
        rig, monkeypatch):
    """
    What the sender must do with each refusal this route can raise. A class raised here and
    named nowhere below is the failure: it would ship classified by whichever side of the
    outbox's 4xx line its status happened to fall on.
    """
    from trixd.common import errors
    from trixd.core.replication import outbox

    # The action is well formed and this controller is not ready for it, against a build
    # that does not serve or accept what the peer named, which no pass settles.
    intended = {"ServiceUnavailable": outbox.QUEUED, "NotFound": outbox.FAILED,
                "BadRequest": outbox.FAILED}
    raised = _apply_route_refusals()

    assert raised, "no refusal was found in the route, so this read the wrong thing"
    unclassified = sorted(raised - set(intended))
    assert not unclassified, (
        f"the apply route raises {unclassified}, and nothing here says what the sender "
        f"should do with it: decide, rather than letting its status code decide")
    for name in sorted(raised):
        status = getattr(errors, name).status_code
        assert _what_the_sender_does_with(status, monkeypatch) == intended[name], (
            f"{name} is {status}, which the sender treats as "
            f"{_what_the_sender_does_with(status, monkeypatch)} rather than "
            f"{intended[name]}")


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


def _applying_plugin(rig, name: str, variable: str, token: str, minimum: str = "") -> None:
    """
    A site plugin with one replicating endpoint, taking its node under a chosen path
    variable and gated either by a node token or by a role.
    """
    import os

    from trixd.common.settings import current as settings
    from trixd.core.plugins import loader

    site = os.path.join(settings().plugins.site_directory, name)
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "manifest.yaml"), "w", encoding="utf-8") as handle:
        handle.write(
            f"plugin: {name}\n"
            "backend: backend\n"
            "description: One replicating endpoint, for testing what apply records.\n"
            "endpoints:\n"
            f"  - path: thing/<{variable}>\n"
            "    methods: [DELETE]\n"
            f"    token: {token}\n"
            + (f"    minimum: {minimum}\n" if minimum else "")
            + "    handler: thing\n"
            "    replicate: true\n"
            "    summary: A replicating endpoint\n")
    with open(os.path.join(site, "backend.py"), "w", encoding="utf-8") as handle:
        handle.write("class Backend:\n"
                     f"    def thing(self, {variable}: str) -> dict:\n"
                     f'        return {{"retired": {variable}}}\n')
    with open(os.path.join(site, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write("")
    assert loader.load(rig, name).state == "loaded"


def test_applying_a_role_gated_endpoint_leaves_a_trail_too(rig, client, headers):
    """
    The row was written only for a node endpoint, so a replicated call gated by a role left
    the peer nothing but the gate's own row for the apply route under the account the pair
    shares - the two controllers' trails then disagree about the same action, and the peer
    cannot say who retired a node's rules. The reason the node case is recorded holds here
    unchanged, so both are.
    """
    from trixd.core.security import audit

    _applying_plugin(rig, "gated", "node", "user", minimum="manager")
    answer = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": "gated", "path": "thing/<node>", "method": "DELETE",
        "variables": {"node": "node002"}, "origin": "ctrl1", "request_id": "r-gated"})
    assert answer.status_code == 200, answer.text

    rows = [row for row in audit.read(limit=100) if row["action"] == "replication.apply"]
    assert rows, "nothing in the trail says a role-gated endpoint was applied for the peer"
    assert rows[0]["object"] == "node002", rows[0]
    assert "ctrl1" in rows[0]["detail"], rows[0]


@pytest.mark.parametrize("variable", NODE_PATH_PARAMS)
def test_the_trail_names_the_node_under_any_name_the_gate_binds(rig, client, variable,
                                                                headers):
    """
    Derived from the gate's own list rather than from a second copy of it: apply looked for
    `node` alone, so the two had already diverged and an endpoint named after one of the
    other two would have recorded the path in place of the node - leaving the trail unable
    to say which node was acted on, with nothing to say so.
    """
    from trixd.core.security import audit

    _applying_plugin(rig, f"named{variable}", variable, "node")
    answer = client.post(f"{API_PREFIX}/system/replication/apply", headers=headers, json={
        "plugin": f"named{variable}", "path": f"thing/<{variable}>", "method": "DELETE",
        "variables": {variable: "node003"}, "origin": "ctrl1",
        "request_id": f"r-{variable}"})
    assert answer.status_code == 200, answer.text

    rows = [row for row in audit.read(limit=100) if row["action"] == "replication.apply"]
    assert rows, f"nothing in the trail names an endpoint taking <{variable}>"
    assert rows[0]["object"] == "node003", \
        (f"the trail records {rows[0]['object']!r} rather than the node for an endpoint "
         f"taking <{variable}>, which the gate binds as well as <node>")


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

    # The sweep may, and does - on the pass the row is next due on. The inline attempt
    # counted, and a row is not offered again until its turn, so with the clock standing
    # still it is the row that has to be moved for this to be the pass that follows.
    handle = database.session()
    try:
        owed = handle.scalar(select(models.Outbox)).id
    finally:
        handle.close()
    age_to_its_first_retry(owed)
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
        row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                               {"node": "node001"}, None, "req", "tester")
        # Aged before each pass, which is what keeps these thirty offers real: a row that
        # has just been offered waits for its turn, so thirty passes over a row nothing
        # ages are one offer and twenty-nine deferrals - and both counts below read the
        # same either way, which is the whole of what this test would have stopped saying.
        for _ in range(30):
            age_past_the_span(row_id)
            outbox.drain("ctrl2")
        assert outbox.states()["failed"] == 0, "a peer merely being away became terminal"
        assert outbox.states()["pending"] == 1, "the row stopped waiting to be sent"
        handle = database.session()
        try:
            row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
            assert row.attempts == 30, \
                f"the peer was offered the row {row.attempts} times over thirty passes, " \
                f"so this is no longer thirty passes of being away"
        finally:
            handle.close()
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


def test_two_merges_at_one_version_keep_both_fields(rig, interleave_once):
    """
    A merge that reads in one session and writes in another is lost at the seam between them:
    both callers read one version, both wrote the whole document, the later one won, and the
    earlier caller was answered 200 with the value it sent rather than the value stored. The
    truncated document then replicates like any other, so the peer agrees with it and no
    sweep ever reports a conflict.
    """
    from trixd.utils import declarations

    declarations.write("prometheus", "config", "", {"rules_directory": "/etc/prometheus"})
    fired = interleave_once(lambda: declarations.merge(
        "prometheus", "config", "", {"reload_command": "systemctl reload prometheus"}, {}))

    stored = declarations.merge("prometheus", "config", "", {"scrape_interval": "30s"}, {})

    assert fired, "the other merge never ran, so nothing interleaved"
    assert stored.get("reload_command") == "systemctl reload prometheus", \
        "the merge that landed first was overwritten, and its caller was answered 200"
    assert stored.get("scrape_interval") == "30s", "the merge that won lost its own field"
    assert stored.get("rules_directory") == "/etc/prometheus", \
        "a field neither caller sent was dropped"
    assert declarations.read("prometheus", "config", "", {}) == stored, \
        "the caller was answered a document that is not the one in the row"


def test_two_merges_of_a_key_that_does_not_exist_yet_keep_both_fields(rig, interleave_once):
    """
    The same seam with no row to read yet, where both callers insert rather than update: the
    unique constraint is what makes the loser read again instead of writing a second row.
    """
    from trixd.utils import declarations

    fired = interleave_once(lambda: declarations.merge(
        "slurm", "config", "", {"cluster_name": "trinity"}, {}))

    stored = declarations.merge("slurm", "config", "", {"partition": "defq"}, {})

    assert fired, "the other merge never ran, so nothing interleaved"
    assert stored.get("cluster_name") == "trinity", \
        "the insert that landed first was overwritten, and its caller was answered 200"
    assert stored.get("partition") == "defq", "the merge that won lost its own field"
    assert len(declarations.listing("slurm", "config")) == 1, \
        "a second row was written for one key"


def _declaration_entry_points() -> dict:
    """
    Every entry point the declarations module offers, read off the module.

    Enumerated rather than listed: the defect below is a precondition that landed on one
    verb and not its siblings, and a test naming the verbs its author knew about is the same
    oversight written twice. A verb added later is driven by this without being mentioned.
    """
    import inspect

    from trixd.utils import declarations

    return {name: value for name, value in sorted(vars(declarations).items())
            if inspect.isfunction(value) and not name.startswith("_")
            and value.__module__ == declarations.__name__}


def _declaration_arguments(function, plugin: str, kind: str, name: str, sent: dict) -> dict:
    """
    One entry point's arguments, built from its signature rather than from its name.

    A parameter this cannot supply is a failure rather than a skip: enumerating a verb and
    then not driving it is how a derived test reads as covering something it never ran.
    """
    import inspect

    known = {"plugin": plugin, "kind": kind, "name": name, "body": dict(sent),
             "changes": dict(sent), "defaults": {}, "fallback": None}
    wanted = list(inspect.signature(function).parameters)
    unknown = [parameter for parameter in wanted if parameter not in known]
    assert not unknown, (
        f"declarations.{function.__name__} takes {unknown}, which this test cannot supply, "
        f"so it would be enumerated and never driven: say what to pass it")
    return {parameter: known[parameter] for parameter in wanted}


def _declaration_version(plugin: str, kind: str, name: str) -> int:
    """
    The version held for one key, read without the module under test.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Declaration).where(
            models.Declaration.plugin == plugin, models.Declaration.kind == kind,
            models.Declaration.name == name))
        return (row.version or 0) if row is not None else 0
    finally:
        handle.close()


@pytest.mark.parametrize("entry_point", sorted(_declaration_entry_points()))
def test_a_declaration_entry_point_that_writes_writes_against_the_version_it_read(
        entry_point, rig, interleave_once):
    """
    Each verb that changes a document orders its write against the version it read.

    A precondition on one verb alone is no precondition at all: a delete ordered against the
    version a change had just superseded stored a deletion as that same version, answered
    its caller 200, and left two documents claiming one version - after which the surviving
    one replicates like any other, so the pair agrees on it and nothing reports a conflict.

    Whether a verb writes is measured here rather than declared, so a reader that starts
    writing is covered and a reader that does not is pinned as not writing.
    """
    from trixd.utils import declarations

    function = _declaration_entry_points()[entry_point]
    plugin, kind, name = "prometheus", "exporter-scope", f"node:{entry_point}"
    declarations.write(plugin, kind, name, {"held": 1})
    before = _declaration_version(plugin, kind, name)

    function(**_declaration_arguments(function, plugin, kind, name, {"probe": 1}))

    if _declaration_version(plugin, kind, name) == before:
        return

    # A competing write lands inside this verb's own read, so the version it read is gone
    # by the time it writes. The row is put back first: the probe above moved it.
    declarations.write(plugin, kind, name, {"held": 1})
    at = _declaration_version(plugin, kind, name)
    fired = interleave_once(
        lambda: declarations.write(plugin, kind, name, {"held": 2, "peer": True}))

    function(**_declaration_arguments(function, plugin, kind, name, {"mine": 1}))

    settled = _declaration_version(plugin, kind, name)
    assert fired, "the competing write never ran, so nothing interleaved"
    assert settled == at + 2, (
        f"the write that landed inside declarations.{entry_point}'s read is version "
        f"{at + 1}, and {entry_point} then stored its own document at version {settled}: "
        f"it wrote against a version that had already moved, so two documents carry one "
        f"version and the caller that lost was answered 200 for a document nobody holds")


def test_a_key_amended_past_the_bound_is_refused_retryably_rather_than_answered_200(
        rig, interleave_once):
    """
    Past the bound nothing was stored, so the caller has to be refused - and refused in a
    way that both reaches it and can be retried.

    A refusal that is not a TrixdError never reaches the handler that turns one into a
    response, so the caller is told nothing it can act on. A refusal below 500 is worse
    than that: the outbox keeps a 5xx queued and makes any other 4xx terminal, so a
    replicated write would be dropped on the peer for good over contention that clears.
    """
    from trixd.common.errors import TrixdError
    from trixd.utils import declarations

    declarations.write("prometheus", "config", "", {"rules_directory": "/etc/prometheus"})
    # A round is only lost to a write that landed, so one landing on every read is what
    # takes the bound; anything less settles and never reaches the refusal.
    fired = interleave_once(
        lambda: declarations.write("prometheus", "config", "",
                                   {"rules_directory": "/etc/prometheus"}),
        times=declarations.ROUNDS)

    with pytest.raises(Exception) as refusal:
        declarations.merge("prometheus", "config", "", {"scrape_interval": "30s"}, {})

    # The preconditions first: without these the refusal could be any other failure, and
    # the assertions below would be measuring something that is not the exhaustion path.
    assert len(fired) == declarations.ROUNDS, \
        f"the other caller ran {len(fired)} times, so the bound is not what refused this"
    assert declarations.read("prometheus", "config", "", {}).get("scrape_interval") is None, \
        "the caller was refused for a change that was in fact stored"
    assert isinstance(refusal.value, TrixdError), \
        f"the exhaustion path raised {type(refusal.value).__name__}, which never reaches " \
        f"the TrixdError handler, so the caller is told nothing it can act on"
    assert refusal.value.status_code >= 500, \
        f"nothing was stored and a retry is what settles it, but the caller was told " \
        f"{refusal.value.status_code}, which the outbox treats as terminal"
    assert "nothing was stored" in refusal.value.message, \
        "the refusal does not say the write did not land"


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
    so the rest of the queue is not offered. Each attempt is a whole timeout spent on a
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


def age_past_the_span(row_id: int):
    """
    Put a row's `created` far enough back that it is past the span and due a new offer.

    Past the span, because that is the state the bounds on a row are about; and past its own
    next offer, because the wait grows with the count and a row that has just been offered
    is not due again until the schedule says. Nothing in a test moves the clock, so the row
    is moved instead.
    """
    from datetime import timedelta

    from trixd.core.replication import outbox

    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        behind = max(outbox.DECLINED_FOR_SECONDS + 60, outbox.due_in(row.attempts) + 1)
        row.created = datetime_now() - timedelta(seconds=behind)
        handle.commit()
    finally:
        handle.close()


def age_to_its_first_retry(row_id: int):
    """
    Put a row's `created` back by one sweep interval, which is the pass a row that has been
    offered once is next due on: the first retry is the interval after the first offer, and
    nothing in a test moves the clock.
    """
    from datetime import timedelta

    from trixd.common.settings import current as settings

    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        row.created = (datetime_now()
                       - timedelta(seconds=settings().replication.interval + 1))
        handle.commit()
    finally:
        handle.close()


def test_the_same_call_offered_twice_is_applied_once(rig, client, headers):
    """
    A repeat of the same call is answered with the answer given the first time, not by
    running the handler again. Delivery is at-least-once, so a peer slower than the sender's
    wait - brief on a request thread, longer on a sweep pass - is offered the row at every
    pass, and replaying the call rather than the answer lands one write repeatedly.
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



def test_a_row_the_peer_answered_for_is_not_offered_again_on_the_next_pass(
        rig, monkeypatch):
    """
    How often a declining row was offered is the span divided by the sweep interval, so a
    site sweeping ten times as often retried one row ten times as hard: how hard one row is
    retried is not a thing a site's interval should decide. Each offer now waits longer
    than the one before, and the wait is this module's.

    What the interval still decides, and must: the first offer and the first retry. A fresh
    row is due at once - which is what leaves the caller's inline path needing no change -
    and its first retry is the pass that follows.
    """
    from datetime import timedelta

    from trixd.common.settings import current as settings
    from trixd.core.replication import outbox

    class Answer:
        """What the apply route answers when its handler declined to act."""
        status_code = 200
        text = ""

        def json(self):
            return {"applied": "prometheus hw-rules/<node>", "origin": "ctrl1",
                    "result": {"node": "node001", "skipped": "not in Luna"}}

    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                           {"node": "node001"}, {"force": True}, "req-backoff", "tester")
    monkeypatch.setattr(outbox.requests, "post", lambda *args, **kwargs: Answer())
    monkeypatch.setattr(outbox, "trust", lambda: True)
    monkeypatch.setattr(outbox, "peer_token", lambda peer, verify, renew=False: "a-token")

    def offers() -> int:
        """How many times the row has been offered, read back between passes."""
        handle = database.session()
        try:
            return handle.scalar(select(models.Outbox)
                                 .where(models.Outbox.id == row_id)).attempts
        finally:
            handle.close()

    outbox.drain("ctrl2")
    assert offers() == 1, \
        f"a row written down this moment was offered {offers()} times on the pass that " \
        f"followed, so a fresh row is not due and the inline path is not what it was"

    outbox.drain("ctrl2")
    assert offers() == 1, \
        f"the row was offered {offers()} times with no time passing at all, so what " \
        f"spaces its offers out is still how often the site sweeps"

    row_interval = settings().replication.interval
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        row.created = datetime_now() - timedelta(seconds=row_interval + 1)
        handle.commit()
    finally:
        handle.close()
    outbox.drain("ctrl2")
    assert offers() == 2, \
        f"an interval after it was written down the row had been offered {offers()} " \
        f"times: the first retry has to stay the pass that follows the first offer"


def test_a_peer_that_is_silent_many_times_is_only_deferred_and_never_given_up_on(
        rig, monkeypatch):
    """
    What makes keying the wait on `attempts` safe, and the one thing it must not cost. The
    count includes silence - a dark peer, a pass holding no token - so a peer that was away
    carries a high count into its next offer. A later offer is all that may follow from the
    count: a row must never go terminal for not having been answered, which is why the span
    counts answers and this does not.

    The deferral is asserted from the attempt count, which both builds have, so a build
    without the wait fails on the assertion rather than on reaching for what it lacks.
    """
    from trixd.core.replication import outbox

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    monkeypatch.setattr(outbox, "_send",
                        lambda peer, row, may_sign_in=True:
                        (outbox.QUEUED, "away", outbox.NO_ANSWER))
    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                           {"node": "node001"}, None, "req-silent", "tester")

    def reading() -> tuple:
        """The row's state and attempt count, read back between passes."""
        handle = database.session()
        try:
            row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
            return row.state, row.attempts
        finally:
            handle.close()

    outbox._failures.clear()
    outbox._open.clear()
    try:
        outbox.drain("ctrl2")
        assert reading() == (outbox.QUEUED, 1), \
            f"the row reads {reading()} after one pass at a peer that said nothing, so " \
            f"the silence the rest of this needs did not happen"
        outbox.drain("ctrl2")
        assert reading() == (outbox.QUEUED, 1), \
            f"the row reads {reading()}: a second offer went out with no time passing, " \
            f"so silence is still offered at whatever rate the site sweeps at"

        # More silent offers than any count in this module, each one due because the row is
        # aged past the span - which is also the only way the count can be driven up here.
        # None of them is an answer, so none of them is a reason to stop asking.
        for _ in range(20):
            age_past_the_span(row_id)
            outbox.drain("ctrl2")
        state, attempts = reading()
        assert attempts >= 21, \
            f"the row was offered {attempts} times, so the count this test needs driven " \
            f"well past every bound in the module was never driven up"
        assert state == outbox.QUEUED, \
            f"{attempts} offers to a peer that never answered left the row {state}: a " \
            f"peer being away is the one thing that is not a reason to stop asking, and " \
            f"a wait keyed on a count that includes silence must not change that"
    finally:
        outbox._failures.clear()
        outbox._open.clear()


def test_a_row_owed_past_the_span_is_still_offered_so_that_it_can_end(rig, monkeypatch):
    """
    A count that did not accumulate under this schedule, which is the one way the wait can
    reach past the span. The pass that gives up on a row is the pass that offers it, so a
    row whose next offer falls further out than anyone will wait would never be offered
    again and so could never be given up on: a row nothing clears and nothing reaps, which
    is the wedge the span was added to remove. The wait for this row's count is hours, and
    an answer further out than one cap is what tells `drain` it is not this schedule's.

    The count is set rather than driven, because that is the state this guards against: a
    row that already carried a high count when the schedule it is now read against arrived,
    which is what an upgrade and a raised INTERVAL both leave behind.
    """
    from datetime import timedelta

    from trixd.core.replication import outbox

    class Answer:
        """What the apply route answers when its handler declined to act."""
        status_code = 200
        text = ""

        def json(self):
            return {"applied": "prometheus hw-rules/<node>", "origin": "ctrl1",
                    "result": {"node": "node001", "skipped": "not in Luna"}}

    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                           {"node": "node001"}, {"force": True}, "req-clamp", "tester")
    monkeypatch.setattr(outbox.requests, "post", lambda *args, **kwargs: Answer())
    monkeypatch.setattr(outbox, "trust", lambda: True)
    monkeypatch.setattr(outbox, "peer_token", lambda peer, verify, renew=False: "a-token")

    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        row.attempts = 20
        row.created = datetime_now() - timedelta(seconds=outbox.DECLINED_FOR_SECONDS + 60)
        handle.commit()
    finally:
        handle.close()

    outbox.drain("ctrl2")
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        assert row.attempts == 21, \
            f"the row has been offered {row.attempts} times: a row the peer has owed for " \
            f"longer than the span was not offered, so nothing can ever give up on it"
        assert row.state == outbox.FAILED, \
            f"the row is {row.state} after the span it may be owed for, so the wait has " \
            f"deferred the exit as well as the retry"
    finally:
        handle.close()

def test_a_row_past_the_span_is_still_spaced_out_in_its_second_hour(rig, monkeypatch):
    """
    The second hour, which is where the growing wait used to stop growing. The wait was
    clamped to the span, and a clamp cannot exceed the age of a row already past the span,
    so such a row was due on every pass: at a thirty-second interval sixteen offers in the
    first hour and a hundred and twenty in the second.

    Driven at a peer that says nothing, because that is who it reaches. A row the peer
    answers for is made terminal by the pass at the span and never sees a second hour; a
    row nothing answers is deliberately never terminal, so it is offered for as long as the
    peer is away, and at what rate is this module's to say.

    Every pass the real sweep would run is run, and the row is aged to its own next due
    point by the passes rather than by hand, so what is measured is a schedule the row
    actually walked. The offers are counted from `attempts`, which both builds keep, so a
    build without the growing wait fails on the count rather than on reaching for a symbol
    it does not have.
    """
    from datetime import timedelta

    from trixd.common.settings import current as settings
    from trixd.core.replication import outbox

    interval = 30
    monkeypatch.setattr(settings().replication, "interval", interval)
    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    monkeypatch.setattr(outbox, "_send",
                        lambda peer, row, may_sign_in=True:
                        (outbox.QUEUED, "away", outbox.NO_ANSWER))
    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                           {"node": "node001"}, None, "req-second-hour", "tester")

    def pass_with_the_row_aged(age: int) -> tuple:
        """One sweep pass with the row that old; answers its state and offer count."""
        handle = database.session()
        try:
            row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
            row.created = datetime_now() - timedelta(seconds=age)
            handle.commit()
        finally:
            handle.close()
        outbox.drain("ctrl2")
        handle = database.session()
        try:
            row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
            return row.state, row.attempts
        finally:
            handle.close()

    outbox._failures.clear()
    outbox._open.clear()
    try:
        first_hour, second_hour, counted = 0, 0, 0
        for age in range(0, 2 * outbox.DECLINED_FOR_SECONDS + 1, interval):
            state, offers = pass_with_the_row_aged(age)
            if offers > counted:
                counted = offers
                if age <= outbox.DECLINED_FOR_SECONDS:
                    first_hour += 1
                else:
                    second_hour += 1
    finally:
        outbox._failures.clear()
        outbox._open.clear()

    # The silence has to have happened and the row has to have survived it, or the two
    # counts below are about something other than a peer that is away.
    assert state == outbox.QUEUED, \
        f"the row is {state} after two hours at a peer that never answered, so this is " \
        f"not the row that is offered for as long as the peer is away"
    assert first_hour > 1 and second_hour > 0, \
        f"the row was offered {first_hour} time(s) in its first hour and {second_hour} " \
        f"in its second, so it was never retried and there is no spacing to measure"

    assert second_hour <= first_hour, \
        f"the row was offered {first_hour} times in its first hour and {second_hour} in " \
        f"its second, so the wait stops growing once the row is older than the span and " \
        f"the spacing is only ever for the first hour"
    allowed = outbox.DECLINED_FOR_SECONDS // outbox.BACKOFF_CAP
    assert second_hour <= allowed, \
        f"the row was offered {second_hour} times in its second hour, where a cap of " \
        f"{outbox.BACKOFF_CAP} s allows {allowed}: the rate past the span is the site's " \
        f"INTERVAL again rather than this module's cap"


def test_no_two_offers_this_schedule_allows_are_further_apart_than_the_cap(rig, monkeypatch):
    """
    What lets the wait stand unclamped, enumerated over the intervals a site can set rather
    than exampled at one of them: no gap the schedule leaves is longer than the cap.

    That is the whole of why the span stays exact. The pass that makes a row terminal is
    the pass that offers it, so a row owed past the span is given up on one cap later at
    worst - where a schedule free to leave a longer gap would defer the exit with the
    retry, which is what clamping the wait to the span was there to prevent and what it
    cost the second hour to do.
    """
    from trixd.common.settings import current as settings
    from trixd.core.replication import outbox

    for interval in (0, 1, 7, 29, 30, 60, 149, 150, 299, 300, 301, 600, 3600):
        monkeypatch.setattr(settings().replication, "interval", interval)
        waits = [outbox.due_in(attempts) for attempts in range(0, 200)]
        gaps = [later - earlier for earlier, later in zip(waits, waits[1:])]
        assert max(gaps) <= outbox.BACKOFF_CAP, \
            f"at INTERVAL={interval} the schedule leaves {max(gaps)} s between two " \
            f"offers against a cap of {outbox.BACKOFF_CAP} s, so a row owed past the " \
            f"span waits that long for the pass that is able to end it"
        assert waits == sorted(waits), \
            f"at INTERVAL={interval} the wait does not grow with the count, so a row is " \
            f"offered harder the longer the peer has not taken it"



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


def test_two_controllers_agree_whatever_order_the_rows_went_in(rig, tmp_path):
    """
    The export is sorted, and that sort is what makes a table hash mean the same thing on
    both controllers. Autoincrement is local - the file says so: "the peer inserts its own
    row and gets its own number" - so the same rows genuinely do sit in different physical
    order on the two sides.

    Without the sort, two controllers holding identical content hash differently, every
    sweep fetches the whole table, the comparison never agrees, and no number of rounds
    resolves it: the rows are already the same, so there is nothing for a merge to change.
    Permanent divergence, and the most expensive outcome this package has.

    Every two-controller test here seeds one side and derives the other by merging, so both
    end up in the same physical order and the sort is never load-bearing in them. This one
    inserts the same rows in different orders on purpose, which is the only way to see it.
    """
    from datetime import datetime

    # One moment for every row, on both sides. Without this the documents differ by their
    # timestamps and the hashes disagree for a reason that has nothing to do with order,
    # which would make this pass against a broken sort.
    when = datetime(2026, 10, 17, 12, 0, 0)
    rows = ("alice", "bob", "carol")

    def seed(handle, order):
        """
        The same three principals, inserted in the order given.
        """
        for name in order:
            handle.add(models.Principal(name=name, role="reader", password_digest="",
                                        enabled=True, version=1, origin="here",
                                        updated_at=when))
        handle.commit()

    here = _peer(tmp_path, "one.db")
    there = _peer(tmp_path, "two.db")
    try:
        seed(here, rows)
        seed(there, tuple(reversed(rows)))

        # The premise: the two really are in different physical order. Read back without an
        # order_by, so this is what the database holds rather than what we asked for.
        order_here = [row.name for row in here.scalars(select(models.Principal)).all()]
        order_there = [row.name for row in there.scalars(select(models.Principal)).all()]
        assert order_here != order_there, \
            f"both sides came out in the same order, so this proves nothing: {order_here}"
        assert sorted(order_here) == sorted(order_there) == sorted(rows), \
            "the two sides do not hold the same rows"

        assert tables.table_hash(here, "principal") == tables.table_hash(there, "principal"), \
            ("two controllers holding identical rows disagree on the table hash, so they "
             "will re-fetch the whole table every sweep and never converge")
    finally:
        here.close()
        there.close()


def test_a_row_refused_since_the_snapshot_is_not_sent_anyway(rig, monkeypatch):
    """
    A drain takes a snapshot and then works through it, which is up to one whole timeout a
    row - minutes with a queue of any size. A caller's own inline attempt can refuse a row
    inside that window, so the state in the snapshot is stale by the time the row is
    reached.

    Re-read, rather than trusted. Taking the snapshot's word counts a refused row as sent,
    leaves its chain unheld, and offers the next action for that node over a prerequisite
    the peer rejected - and the pass reports a clean `sent` with no refusals, so nothing
    says it happened.

    The refusal has to arrive from a second actor, which is what makes this different from
    the pass next door: a refusal that happens inside _send leaves the row queued in the
    snapshot and queued when it is read back, so it never exercises the re-read.
    """
    from trixd.core.replication import outbox

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    offered = []

    def send(peer, row, may_sign_in=True):
        """
        A peer that takes everything. While it handles node002, somebody else refuses
        node001's delete - a caller's inline attempt, landing inside the drain's window.
        """
        offered.append((row.method, row.path, row.payload))
        if "node002" in row.payload:
            handle = database.session()
            try:
                stale = handle.scalar(select(models.Outbox).where(
                    models.Outbox.request_id == "req-delete"))
                stale.state = outbox.FAILED
                stale.message = "400: the peer refused it"
                handle.commit()
            finally:
                handle.close()
        return outbox.DONE, "", outbox.ANSWERED

    monkeypatch.setattr(outbox, "_send", send)
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                  {"node": "node002"}, None, "req-other", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "DELETE",
                  {"node": "node001"}, None, "req-delete", "tester")
    outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/repair", "POST",
                  {"node": "node001"}, None, "req-repair", "tester")

    outbox._failures.clear()
    outbox._open.clear()
    try:
        result = outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()

    assert any("node002" in one[2] for one in offered), \
        "node002 never went, so the second actor never ran and this proves nothing"
    assert not any(one[0] == "DELETE" and "node001" in one[2] for one in offered), \
        f"a row refused since the snapshot was offered to the peer anyway: {offered}"
    assert not any("repair" in one[1] for one in offered), \
        f"the next action for that node went over a refused prerequisite: {offered}"
    assert result["blocked"] == 1, f"node001's chain was not held: {result}"

    handle = database.session()
    try:
        states = {row.request_id: row.state for row in handle.scalars(
            select(models.Outbox).where(models.Outbox.peer == "ctrl2")).all()}
    finally:
        handle.close()
    assert states.get("req-delete") == outbox.FAILED, \
        f"the refusal was overwritten by the drain: {states}"
    assert states.get("req-repair") == outbox.QUEUED, \
        f"the action behind it did not stay queued: {states}"


def test_a_status_code_the_peer_answers_with_is_not_asked_for_ever(rig, monkeypatch):
    """
    The same rule as a decline, for the other way the peer answers and does not do it. A
    row still queued is cleared by nothing - the next action for a node clears only a
    refusal, and the reap collects only a refusal - so a 5xx, a 401 or a 403 rode the sweep
    for ever and held its node's later actions with it. Answering that way for long enough
    makes it terminal, which is what gives it both exits.

    Ordinary rather than exotic: a peer whose Prometheus has not scraped a node yet answers
    502, so the row that marks the node's chain stuck is one a later scrape would take.

    Both halves are asserted, because a 5xx is often a moment the next pass clears: a fresh
    row answered 502 once stays queued, and one answered that way for longer than it is
    asked for goes terminal and is then let go of with what it was holding. Driven through
    the real send, so the status code is turned into an outcome by the code that will do it
    in the field.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from trixd.core.replication import outbox
    from trixd.db import engine as database
    from trixd.db import models

    class Answer:
        """What the apply route answers when its handler could not reach what it needs."""
        status_code = 502
        text = "prometheus has not scraped node001 yet"

        def json(self):
            return {}

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    row_id = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                           {"node": "node001"}, {"force": True}, "req-502", "tester")
    # Queued behind it for the same node, which is what the hold is for and what has to be
    # let go of with it: a repair relabels a file the refused write never made.
    behind = outbox.submit("ctrl2", "prometheus", "hw-rules/<node>/actions/repair", "POST",
                           {"node": "node001"}, None, "req-behind", "tester")
    monkeypatch.setattr(outbox.requests, "post", lambda *args, **kwargs: Answer())
    monkeypatch.setattr(outbox, "trust", lambda: True)
    monkeypatch.setattr(outbox, "peer_token", lambda peer, verify, renew=False: "a-token")
    outbox._failures.clear()
    outbox._open.clear()
    try:
        first = outbox.drain("ctrl2")
        handle = database.session()
        try:
            young = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
            assert young.state == outbox.QUEUED, \
                f"one 502 on a fresh row made it {young.state}"
            assert first["blocked"] == 1, f"node001's repair was not held: {first}"
            # Both rows go back far enough that the refusal, when it is written down, is
            # newer than what was already queued behind it. That is the field's order, and
            # it is the order the reap looks for: it takes only what was waiting when the
            # refusal happened, because anything newer would have cleared the refusal.
            held = handle.scalar(select(models.Outbox).where(models.Outbox.id == behind))
            young.created = datetime_now() - timedelta(seconds=outbox.KEEP_REFUSED + 7200)
            held.created = datetime_now() - timedelta(seconds=outbox.KEEP_REFUSED + 7100)
            handle.commit()
        finally:
            handle.close()

        aged = outbox.drain("ctrl2")
        handle = database.session()
        try:
            row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
            assert row.state == outbox.FAILED, \
                f"after {row.attempts} answers of 502 the row is still {row.state} and " \
                f"the pass still holds node001 ({aged}), so nothing will ever clear it"
            # The exit the terminal state is for: a day with nobody acting on the node.
            row.finished = datetime_now() - timedelta(seconds=outbox.KEEP_REFUSED + 60)
            handle.commit()
        finally:
            handle.close()

        # The pass that lets go of it, and then the one that finds the node free: the reap
        # runs at the end of a pass, so the pass that does it has already counted the hold.
        outbox.drain("ctrl2")
        free = outbox.drain("ctrl2")
        handle = database.session()
        try:
            left = {row.id for row in handle.scalars(select(models.Outbox)).all()}
        finally:
            handle.close()
    finally:
        outbox._failures.clear()
        outbox._open.clear()
    assert row_id not in left, \
        f"the row the peer answered 502 for is still there after being let go of: {left}"
    assert behind not in left, \
        f"node001's repair outlived the row it was held behind: {left}"
    assert free["blocked"] == 0 and free["left"] == 0, \
        f"node001 is still held after the reap: {free}"


def test_an_inline_send_waits_for_what_is_already_owed_for_that_node(rig, monkeypatch):
    """
    The inline send holds a node's chain or it undoes the sweep's ordering. `drain` offers a
    node's oldest owed action first because the later ones assume it landed; the inline path
    offered whatever had just been written down, whatever else was owed for that node.

    So a write the peer answered 502 for, followed by a delete for the same node delivered
    at once, left the sweep to apply the write over the delete: the peer kept rule files for
    a node that is gone, both rows read `done`, and health was green. What is asserted is
    the order the peer saw, which is the only thing that cannot come out the same either
    way.
    """
    from trixd.core.replication import outbox

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    applied_at_peer = []
    refused_once = []

    def send(peer, row, may_sign_in=True):
        """
        A peer whose Prometheus has not scraped node001 yet, once, and takes the rest.
        """
        if row.method == "PATCH" and not refused_once:
            refused_once.append(row.path)
            return outbox.QUEUED, "502: not scraped yet", outbox.ANSWERED
        applied_at_peer.append(row.method)
        return outbox.DONE, "", outbox.ANSWERED

    monkeypatch.setattr(outbox, "_send", send)
    outbox._failures.clear()
    outbox._open.clear()
    try:
        wrote = outbox.replicate("prometheus", "hw-rules/<node>", "PATCH",
                                 {"node": "node001"}, {"force": True}, {"changed": True},
                                 "req-write", "tester")
        # The precondition, not the outcome: the write really was offered inline and really
        # was not taken. Without that the ordering this pins never arises.
        assert refused_once, "the write was never offered inline, so nothing is owed"
        assert wrote["peers"] == {"ctrl2": outbox.QUEUED}, wrote

        deleted = outbox.replicate("prometheus", "hw-rules/<node>", "DELETE",
                                   {"node": "node001"}, None, {"changed": True},
                                   "req-delete", "tester")
        # Whatever the sweep has not delivered yet, it delivers on the pass that row is
        # next due on: the write was offered inline and refused, which counted, so with
        # the clock standing still the row is what has to be moved.
        handle = database.session()
        try:
            owed = handle.scalar(select(models.Outbox)
                                 .where(models.Outbox.method == "PATCH")).id
        finally:
            handle.close()
        age_to_its_first_retry(owed)
        outbox.drain("ctrl2")
    finally:
        outbox._failures.clear()
        outbox._open.clear()

    assert applied_at_peer == ["PATCH", "DELETE"], \
        f"the peer saw {applied_at_peer}: the delete was delivered inline while the write " \
        f"for the same node was still owed, so the sweep applied the write over it and " \
        f"the peer keeps rule files for a node that is gone"
    assert deleted["peers"] == {"ctrl2": "held-behind"}, \
        f"the delete reported {deleted['peers']}, which does not say it was held"


def test_a_first_action_for_a_node_is_sent_inline_rather_than_held(rig, monkeypatch):
    """
    The other half of the hold, and the half that would be silent: a guard that waits for a
    row that is not there would take every inline send off the request path and leave every
    call to the sweep, which is the latency the inline attempt exists to avoid.
    """
    from trixd.core.replication import outbox

    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    monkeypatch.setattr(outbox, "_send",
                        lambda peer, row, may_sign_in=True: (outbox.DONE, "",
                                                             outbox.ANSWERED))
    outbox._failures.clear()
    outbox._open.clear()
    try:
        said = outbox.replicate("prometheus", "hw-rules/<node>", "PATCH",
                                {"node": "node001"}, {"force": True}, {"changed": True},
                                "req-first", "tester")
        # A second node, which the first says nothing about.
        elsewhere = outbox.replicate("prometheus", "hw-rules/<node>", "DELETE",
                                     {"node": "node002"}, None, {"changed": True},
                                     "req-other", "tester")
        # And the same node again, once what was owed for it has been taken.
        again = outbox.replicate("prometheus", "hw-rules/<node>/actions/repair", "POST",
                                 {"node": "node001"}, None, {"changed": True},
                                 "req-again", "tester")
    finally:
        outbox._failures.clear()
        outbox._open.clear()
    assert said["peers"] == {"ctrl2": outbox.DONE}, said
    assert elsewhere["peers"] == {"ctrl2": outbox.DONE}, \
        f"node002 was held behind node001: {elsewhere}"
    assert again["peers"] == {"ctrl2": outbox.DONE}, \
        f"node001 was held behind a row that is already done: {again}"


def test_the_drain_waits_as_long_as_the_sweep_rather_than_as_a_caller_would(rig,
                                                                           monkeypatch):
    """
    Three seconds is the inline path's number, chosen because a caller's thread is held for
    it. The drain inherited it, and a peer that is slow rather than dark then drained zero
    rows per pass for ever: it did the work every pass, answered after the sender had gone,
    and the silence opened its own breaker on a peer that was answering.

    Nobody is waiting on a sweep pass, so it pays the sweep's own timeout. What keeps a pass
    away from a peer that really is dark is the drain stopping at the first silence and the
    breaker, not the clock on one offer - one wait per pass, not one per row.
    """
    from trixd.core.replication import outbox, sweep

    class Answer:
        """A peer that takes it."""
        status_code = 200
        text = ""

        def json(self):
            return {"applied": "prometheus hw-rules/<node>", "origin": "ctrl1",
                    "result": {"node": "node001"}}

    waited = []
    monkeypatch.setattr(outbox, "peers", lambda: ["ctrl2"])
    monkeypatch.setattr(outbox.requests, "post",
                        lambda *args, **kwargs: waited.append(kwargs["timeout"])
                        or Answer())
    monkeypatch.setattr(outbox, "trust", lambda: True)
    monkeypatch.setattr(outbox, "peer_token", lambda peer, verify, renew=False: "a-token")
    monkeypatch.setattr(outbox, "held_token", lambda peer: "a-token")
    outbox._failures.clear()
    outbox._open.clear()
    try:
        outbox.submit("ctrl2", "prometheus", "hw-rules/<node>", "PATCH",
                      {"node": "node001"}, {"force": True}, "req-slow", "tester")
        outbox.drain("ctrl2")
        assert waited == [sweep.TIMEOUT], \
            f"the drain gave the peer {waited}s: a peer slower than that drains nothing, " \
            f"pass after pass, and opens its own breaker on work it has done"

        # The inline path keeps its own, because the caller is waiting on it.
        outbox.replicate("prometheus", "hw-rules/<node>", "DELETE",
                         {"node": "node002"}, None, {"changed": True}, "req-inline",
                         "tester")
    finally:
        outbox._failures.clear()
        outbox._open.clear()
    assert waited[1:] == [outbox.TIMEOUT], \
        f"a caller's thread was held for {waited[1:]}s, not the inline path's " \
        f"{outbox.TIMEOUT}s"
