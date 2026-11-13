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
Where the books come from: the archive fetched into the checkout when it is not there,
unpacked safely, and the first step of the build unit that does it, with the network
replaced by an archive in memory.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import io
import json
import os
import shutil
import tarfile
import urllib.error

import pytest

from trixd.common.constants import API_PREFIX
from trixd.common.errors import ServiceUnavailable
from trixd.plugins.osimage import checkout, jobs, runner

from tests.unit.test_osimage_plugin import (REQUEST_ID, _create, _no_supervisor_thread,  # noqa: F401
                                            plugin)
from tests.unit.test_osimage_runner import _job, _result, sigterm_restored  # noqa: F401
from tests.unit.test_osimage_supervisor import children  # noqa: F401

URL = "https://example.invalid/archive/main.tar.gz"
TOP = "trinityX-main"
SCRATCH = ".checkout-"


def _member(name: str, kind=tarfile.REGTYPE, data: bytes = b"", link: str = "", mode: int = 0o644):
    """
    One archive member, ready to be added.
    """
    member = tarfile.TarInfo(name)
    member.type, member.mode, member.linkname = kind, mode, link
    member.size = len(data) if kind == tarfile.REGTYPE else 0
    return member, io.BytesIO(data) if kind == tarfile.REGTYPE else None


def _archive(*members) -> bytes:
    """
    A gzip archive of the members given as _member made them.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for member, data in members:
            bundle.addfile(member, data)
    return buffer.getvalue()


def _books(*extra) -> bytes:
    """
    What the repository's archive looks like: a top directory with the books in it.
    """
    return _archive(
        _member(TOP, tarfile.DIRTYPE, mode=0o775),
        _member(f"{TOP}/site", tarfile.DIRTYPE, mode=0o775),
        _member(f"{TOP}/site/ansible.cfg", data=b"[defaults]\n"),
        _member(f"{TOP}/site/run.sh", data=b"#!/bin/sh\n", mode=0o755),
        _member(f"{TOP}/site/group_vars", tarfile.DIRTYPE, mode=0o775),
        _member(f"{TOP}/site/group_vars/all.yml.example", data=b"x: 1\n"),
        _member(f"{TOP}/site/imports", tarfile.DIRTYPE, mode=0o775),
        _member(f"{TOP}/site/imports/group_vars", tarfile.SYMTYPE, link="../group_vars"),
        *extra)


class Net:
    """
    urlopen in place of the network: it answers an archive, or an error, and counts.
    """

    def __init__(self, answer) -> None:
        """
        The bytes to answer, or the exception to raise.
        """
        self.answer = answer
        self.asked: list = []

    def __call__(self, url, timeout=None):
        """
        One request; the url and the timeout it was made with are kept.
        """
        self.asked.append((url, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return io.BytesIO(self.answer)


@pytest.fixture
def net(monkeypatch):
    """
    A network that answers the books archive until a test says otherwise.
    """
    answering = Net(_books())
    monkeypatch.setattr(checkout.urllib.request, "urlopen", answering)
    return answering


def _ignore(message: str) -> None:
    """
    A fetch whose lines nobody reads.
    """


def _left_beside(repo) -> list:
    """
    What is in the directory that holds the checkout, other than the checkout.
    """
    return sorted(name for name in os.listdir(repo.parent) if name != repo.name)


def test_a_missing_checkout_is_fetched_with_its_top_directory_stripped(tmp_path, net):
    """
    The archive is unpacked into the checkout itself, so site is directly below it; modes
    are kept, a link inside the tree stays a link, and the download leaves nothing beside it.
    """
    repo = tmp_path / "trinityx-combined"
    told = []
    assert checkout.ensure(str(repo), URL, told.append) is True
    assert told == [f"fetching the books from {URL}", "unpacked"]
    assert (repo / "site" / "ansible.cfg").read_text() == "[defaults]\n"
    assert not (repo / TOP).exists()
    assert os.access(repo / "site" / "run.sh", os.X_OK) and not os.access(repo / "site" / "ansible.cfg", os.X_OK)
    assert os.readlink(repo / "site" / "imports" / "group_vars") == "../group_vars"
    assert (repo / "site" / "imports" / "group_vars" / "all.yml.example").read_text() == "x: 1\n"
    assert net.asked == [(URL, checkout.SOCKET_TIMEOUT)]
    assert _left_beside(repo) == []


def test_a_checkout_that_is_there_is_never_fetched_over(tmp_path, net):
    """
    The cluster's own checkout is not touched, and nothing is downloaded for it.
    """
    repo = tmp_path / "trinityx-combined"
    (repo / "site").mkdir(parents=True)
    (repo / "site" / "mine").write_text("kept")
    told = []
    assert checkout.ensure(str(repo), URL, told.append) is False
    assert (repo / "site" / "mine").read_text() == "kept" and net.asked == [] and told == []


def test_the_cluster_answers_are_never_taken_from_a_download(tmp_path, net):
    """
    An archive that carries site/group_vars/all.yml has everything else unpacked and that
    file skipped, so what ansible finds there can only be the cluster's own.
    """
    net.answer = _books(_member(f"{TOP}/site/group_vars/all.yml", data=b"trix_version: from-download\n"))
    repo = tmp_path / "trinityx-combined"
    assert checkout.ensure(str(repo), URL, _ignore) is True
    assert (repo / "site" / "group_vars" / "all.yml.example").is_file()
    assert not (repo / "site" / "group_vars" / "all.yml").exists()
    assert not checkout.has_site_answers(str(repo))


@pytest.mark.parametrize("escape, said", [
    (_member(f"{TOP}/../../evil", data=b"x"), "leads out of its directory"),
    (_member(f"{TOP}/site/../../../evil", data=b"x"), "leads out of its directory"),
    (_member("/etc/evil", data=b"x"), "leads out of its directory"),
    (_member(f"{TOP}/site/out", tarfile.SYMTYPE, link="../../.."), "which leaves its directory"),
    (_member(f"{TOP}/site/out", tarfile.SYMTYPE, link="/etc"), "which leaves its directory"),
    (_member(f"{TOP}/site/hard", tarfile.LNKTYPE, link=f"{TOP}/site/ansible.cfg"),
     "neither a file, a directory nor a link"),
    (_member(f"{TOP}/site/pipe", tarfile.FIFOTYPE), "neither a file, a directory nor a link"),
    (_member("stray", data=b"x"), "outside a top directory"),
])
def test_an_archive_that_leads_out_of_its_directory_is_refused_whole(tmp_path, net, escape, said):
    """
    A path with .., an absolute one, a link that leaves, and a member that is not a file, a
    directory or a link each refuse the whole archive: nothing is put at the checkout,
    nothing is written outside the scratch directory, and nothing is left beside it.
    """
    net.answer = _books(escape)
    repo = tmp_path / "inside" / "trinityx-combined"
    with pytest.raises(ServiceUnavailable) as refused:
        checkout.ensure(str(repo), URL, _ignore)
    assert said in refused.value.message, refused.value.message
    assert not repo.exists() and not (tmp_path / "evil").exists() and not (tmp_path / "inside" / "evil").exists()
    assert _left_beside(repo) == []


@pytest.mark.parametrize("after, said", [
    ((), "to B/.., which leaves its directory"),
    ((_member(f"{TOP}/site/A/evil", data=b"x"),), "through a link that leaves its directory"),
])
def test_links_that_lead_out_only_once_the_next_one_exists_are_caught(tmp_path, net, after, said):
    """
    A link to a name that is not there yet passes when it is made, and leads out of the
    tree when the link it names appears. Judged as the unpacked tree stands, it refuses the
    archive, and a file named below it is not written outside.
    """
    net.answer = _books(
        _member(f"{TOP}/site/A", tarfile.SYMTYPE, link="B/.."),
        _member(f"{TOP}/site/B", tarfile.SYMTYPE, link=".."),
        *after)
    repo = tmp_path / "inside" / "trinityx-combined"
    with pytest.raises(ServiceUnavailable) as refused:
        checkout.ensure(str(repo), URL, _ignore)
    assert said in refused.value.message, refused.value.message
    assert not repo.exists() and not (tmp_path / "evil").exists() and not (tmp_path / "inside" / "evil").exists()
    assert _left_beside(repo) == []


def test_an_absent_repo_url_fetches_nothing_and_says_so(tmp_path, net):
    """
    With no address to fetch from the missing checkout is a 503 naming both, and the
    network is not asked.
    """
    repo = tmp_path / "trinityx-combined"
    with pytest.raises(ServiceUnavailable) as refused:
        checkout.ensure(str(repo), "", _ignore)
    assert f"the books are not at {repo} and repo_url is empty" in refused.value.message
    assert net.asked == [] and not repo.exists()


@pytest.mark.parametrize("url", ["http://example.invalid/books.tar", "file:///etc/passwd",
                                 "ftp://example.invalid/books.tar", "books.tar"])
def test_only_an_https_address_is_fetched(tmp_path, net, url):
    """
    The books run as root, so plain http, a local path or another scheme is refused
    before anything is opened.
    """
    with pytest.raises(ServiceUnavailable) as refused:
        checkout.ensure(str(tmp_path / "trinityx-combined"), url, _ignore)
    assert "must be an https address" in refused.value.message
    assert net.asked == []


@pytest.mark.parametrize("answer, said", [
    (urllib.error.URLError("Name or service not known"), "cannot fetch"),
    (OSError("connection reset"), "cannot fetch"),
    (b"this is not an archive", "the archive cannot be read"),
    (_archive(_member(f"{TOP}/README", data=b"x")), "has no site directory"),
])
def test_a_download_that_fails_leaves_nothing_for_the_next_start_to_mistake_for_the_books(
        tmp_path, net, answer, said):
    """
    A network failure, a body that is no archive and an archive without books each end as
    a 503, with no checkout and no scratch directory left behind.
    """
    net.answer = answer
    repo = tmp_path / "inside" / "trinityx-combined"
    with pytest.raises(ServiceUnavailable) as refused:
        checkout.ensure(str(repo), URL, _ignore)
    assert said in refused.value.message, refused.value.message
    assert not repo.exists() and _left_beside(repo) == []


def test_a_download_is_bounded_in_size_and_in_time(tmp_path, net, monkeypatch):
    """
    An answer larger than the limit, or one that takes longer than the time allowed, is
    given up on rather than filling the disk or holding the request for ever.
    """
    repo = tmp_path / "inside" / "trinityx-combined"
    monkeypatch.setattr(checkout, "ARCHIVE_MOST", 100)
    with pytest.raises(ServiceUnavailable) as large:
        checkout.ensure(str(repo), URL, _ignore)
    assert "is larger than 100 bytes" in large.value.message
    monkeypatch.setattr(checkout, "ARCHIVE_MOST", 10 ** 9)
    monkeypatch.setattr(checkout, "FETCH_TIMEOUT", -1)
    with pytest.raises(ServiceUnavailable) as slow:
        checkout.ensure(str(repo), URL, _ignore)
    assert "did not arrive within -1 s" in slow.value.message
    assert not repo.exists() and _left_beside(repo) == []


def test_an_archive_that_unpacks_to_more_than_the_limit_is_refused_whole(tmp_path, net, monkeypatch):
    """
    A small download can expand to fill the disk: one file past the limit, or many that
    pass it together, end the unpack with nothing left at the checkout or beside it.
    """
    repo = tmp_path / "inside" / "trinityx-combined"
    # raising=False: a build without the limit ignores it and the test then fails on its assertion.
    monkeypatch.setattr(checkout, "EXTRACTED_MOST", 1000, raising=False)
    for books in (_books(_member(f"{TOP}/site/big", data=b"0" * 5000)),
                  _books(*[_member(f"{TOP}/site/part{number}", data=b"0" * 300) for number in range(5)])):
        net.answer = books
        with pytest.raises(ServiceUnavailable) as refused:
            checkout.ensure(str(repo), URL, _ignore)
        assert "unpacks to more than 1000 bytes" in refused.value.message
        assert not repo.exists() and _left_beside(repo) == []
    net.answer = _books(_member(f"{TOP}/site/fits", data=b"0" * 400))
    assert checkout.ensure(str(repo), URL, _ignore) is True


def test_a_scratch_directory_a_killed_fetch_left_is_swept_by_the_next(tmp_path, net):
    """
    A unit stopped hard mid-unpack leaves its scratch directory; the next fetch removes it
    so nothing half unpacked lingers beside the checkout.
    """
    repo = tmp_path / "trinityx-combined"
    (tmp_path / f"{SCRATCH}dead" / "tree").mkdir(parents=True)
    assert checkout.ensure(str(repo), URL, _ignore) is True
    assert _left_beside(repo) == []


def test_a_create_downloads_nothing(plugin, tmp_path, net):  # noqa: F811
    """
    A create with the checkout missing answers at once with the unit started and the
    address in the job for it: the fetch is the unit's.
    """
    client, headers, commands = plugin
    repo = tmp_path / "fresh" / "trinityx-combined"
    settings = {"repo": str(repo), "repo_url": URL}
    changed = client.patch(f"{API_PREFIX}/osimage/settings", json=settings, headers=headers)
    assert changed.status_code == 200, changed.text
    started = _create(client, headers, tmp_path)
    assert started.status_code == 200, started.text
    assert net.asked == [] and not repo.exists()
    state = jobs.read_state(jobs.path(str(tmp_path / "state"), "rocky9"))
    assert state["state"] == "running" and state["request_id"] == REQUEST_ID
    assert jobs.read_json(jobs.path(str(tmp_path / "state"), "rocky9"), jobs.REQUEST)[
        "settings"]["repo_url"] == URL


def _unfetched(tmp_path, monkeypatch, url: str = URL) -> tuple:
    """
    A build whose checkout is not there and whose address is given: the job directory,
    the checkout and what the book was started with.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, kernels=("5.14.0-503.el9",))
    shutil.rmtree(tmp_path / "repo")
    backup = tmp_path / "backup" / "all.yml"
    backup.parent.mkdir()
    backup.write_text("trix_version: 15\n")
    request = jobs.read_json(jobdir, jobs.REQUEST)
    request["settings"]["repo_url"] = url
    jobs.files.write(os.path.join(jobdir, jobs.REQUEST), json.dumps(request))
    return jobdir, tmp_path / "repo", started


def _said(jobdir: str) -> list:
    """
    The messages the build relayed, in order.
    """
    return [line["message"] for line in jobs.lines_since(jobdir)[0]]


def test_the_first_step_of_the_build_fetches_the_books_and_says_so(tmp_path, monkeypatch, net,  # noqa: F811
                                                                   sigterm_restored):  # noqa: F811
    """
    A build whose checkout is missing fetches it before anything else: the first two lines
    relayed are the fetch and the unpack, and the book then runs on what was unpacked.
    """
    jobdir, repo, started = _unfetched(tmp_path, monkeypatch)
    net.answer = _books(_member(f"{TOP}/site/ansible.cfg", data=b"[defaults]\ninventory = hosts\n"),
                        _member(f"{TOP}/site/plugins", tarfile.DIRTYPE),
                        _member(f"{TOP}/site/plugins/connection", tarfile.DIRTYPE),
                        _member(f"{TOP}/site/plugins/connection/lchroot.py", data=b""))
    assert runner.Build(jobdir).run() == 0
    assert _said(jobdir)[:2] == [f"fetching the books from {URL}", "unpacked"]
    assert net.asked == [(URL, checkout.SOCKET_TIMEOUT)] and (repo / "site" / "ansible.cfg").is_file()
    (argv, kwargs), = started
    assert kwargs["cwd"] == str(repo / "site")


def _scratch_beside(repo) -> list:
    """
    The scratch directories of a fetch that are beside the checkout.
    """
    return [name for name in os.listdir(repo.parent) if name.startswith(SCRATCH)]


@pytest.mark.parametrize("answer, said", [
    (urllib.error.URLError("Name or service not known"), f"cannot fetch {URL}"),
    (_archive(_member(f"{TOP}/README", data=b"x")), "has no site directory"),
])
def test_a_fetch_that_fails_ends_the_build_with_a_reason_and_leaves_nothing(
        tmp_path, monkeypatch, net, sigterm_restored, answer, said):  # noqa: F811
    """
    No download, or an archive without books, ends the build before the book with the cause
    as the reason, which the push then relays as the failure line; no checkout and no
    scratch directory remain, and the lines relayed say what was tried.
    """
    jobdir, repo, started = _unfetched(tmp_path, monkeypatch)
    net.answer = answer
    assert runner.Build(jobdir).run() == 1
    assert said in _result(jobdir)["reason"], _result(jobdir)["reason"]
    assert _said(jobdir)[0] == f"fetching the books from {URL}"
    assert started == [] and not repo.exists() and _scratch_beside(repo) == []


def test_books_without_the_connection_plugin_end_the_build_naming_the_file(
        tmp_path, monkeypatch, net, sigterm_restored):  # noqa: F811
    """
    What the book needs of the checkout is checked once it is there, as it was when the
    start fetched it, so a half-made checkout fails here and not an hour into the book.
    """
    jobdir, repo, started = _unfetched(tmp_path, monkeypatch)
    assert runner.Build(jobdir).run() == 1
    assert "plugins/connection/lchroot.py missing" in _result(jobdir)["reason"]
    assert started == []


@pytest.mark.parametrize("url, said", [
    ("", "repo_url is empty: put the checkout there, or set repo_url in the osimage settings to the "
         "https archive of a TrinityX tag to fetch them from"),
    ("http://example.invalid/books.tar.gz", "repo_url must be an https address, not http://"),
])
def test_a_build_with_no_checkout_and_no_address_to_fetch_one_from_ends_saying_so(
        tmp_path, monkeypatch, net, sigterm_restored, url, said):  # noqa: F811
    """
    Nothing to build with and nothing to fetch it from, or an address held from before
    the https rule, is the reason the build failed, said before any source is read, and
    nothing is fetched.
    """
    jobdir, repo, started = _unfetched(tmp_path, monkeypatch, url)
    assert runner.Build(jobdir).run() == 1
    assert said in _result(jobdir)["reason"], _result(jobdir)["reason"]
    assert started == [] and net.asked == [] and not repo.exists()


@pytest.mark.parametrize("url", ["http://example.invalid/books.tar.gz", "file:///etc"])
def test_a_repo_url_that_is_not_an_https_address_is_refused_at_the_settings(plugin, url):  # noqa: F811
    """
    Only an https address, or none, can be set: the books run as root, so neither plain
    http nor a file is ever fetched.
    """
    client, headers, _ = plugin
    refused = client.patch(f"{API_PREFIX}/osimage/settings", json={"repo_url": url}, headers=headers)
    assert refused.status_code == 400 and "repo_url must be an https address" in refused.text
    assert client.patch(f"{API_PREFIX}/osimage/settings", json={"repo_url": ""},
                        headers=headers).status_code == 200


def test_a_held_http_address_is_no_obstacle_while_the_checkout_is_there(tmp_path, monkeypatch, net,
                                                                       sigterm_restored):  # noqa: F811
    """
    An address held from before the https rule is never used while the checkout is there,
    so the build runs on the checkout and fetches nothing.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, kernels=("5.14.0-503.el9",))
    request = jobs.read_json(jobdir, jobs.REQUEST)
    request["settings"]["repo_url"] = "http://example.invalid/books.tar.gz"
    jobs.files.write(os.path.join(jobdir, jobs.REQUEST), json.dumps(request))
    assert runner.Build(jobdir).run() == 0
    assert len(started) == 1 and net.asked == []
