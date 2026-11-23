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
The image build runner, its job directory and its mounts, with no ansible, no lchroot,
no root and no image tree: the book is a fake child, the probes answer as a ready
controller does, the mount table is a stub, and the sources are trees of the scratch
directory.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import datetime
import json
import os
import signal
import sys
import time

import pytest

from trixd.common.errors import BackendFailure, BadRequest, Conflict
from trixd.plugins.osimage import detect, jobs, mounts, rules, runner

from tests.unit.test_osimage_detect import NOBLE_RELEASE, put, rocky_root

SETTINGS = {"repo": "/trinity/trinityx-combined", "repo_url": "", "answers": "/trinity/local/backup/group_vars/all.yml",
            "images_root": "/trinity/images", "python": "/trinity/local/python",
            "collections": "/trinity/local/trixd/collections",
            "builder": "books", "stub_gap_seconds": 3}
# What Luna's create writes down: its own stream, tree and record words, and the sources
# the body or the mirrors map named, or none.
REQUEST = {"name": "rocky9", "request_id": "1761818733.1234567891234", "path": "/trinity/images/rocky9",
           "distribution": "redhat", "osrelease": "9.6", "architecture": "x86_64", "media": None,
           "mirrors_from": None, "settings": SETTINGS}
# The settings the runner works with: the request's, and the books of the checkout.
BUILT = dict(SETTINGS, books="/trinity/trinityx-combined/site")
# What the controller the unit runs on says of itself, where a test needs one.
CONTROLLER = {"family": "redhat", "distro": "rocky", "release": "9", "dotrelease": "9.6"}
# What the books of every family import and name, as the installer role lays them out.
COLLECTIONS = tuple(sorted({name for rule in rules.FAMILIES.values() for name in rule["collections"]}))
GALAXY_ROLE = rules.FAMILIES["redhat"]["galaxy_roles"][0]
# The sources a job is given unless a test names its own: a Rocky 9.8 mirror root.
MIRROR = "a Rocky 9.8 mirror root"
VERDICT = {"family": "redhat", "playbook": "compute-redhat.yml", "arch": "x86_64", "base": 0,
           "base_variable": "image_el_baseurl", "skip_tags": [],
           "variables": {"image_el_releasever": "9",
                         "image_el_baseurl": "https://download.rockylinux.org/pub/rocky/9/"},
           "sources": [{"source": "https://download.rockylinux.org/pub/rocky/9/",
                        "kind": "repo-root", "role": "base"}]}

# What ansible's default callback prints, padded to the terminal width it assumes.
EXCERPT = """
PLAY [localhost] ***************************************************************

TASK [Set image name] **********************************************************
ok: [localhost]

PLAY [controllers] *************************************************************

TASK [trinity/image-create : Bootstrap] ****************************************
changed: [localhost]

TASK [luna/osimage-create : Verify if image already exists in luna] ************
fatal: [localhost]: FAILED! => {"changed": true, "cmd": "luna osimage show img", "rc": 1}
...ignoring

TASK [trinity/image-create : Try the fast path] ********************************
fatal: [localhost]: FAILED! => {"msg": "no fast path"}

TASK [trinity/image-create : Rescue] *******************************************
ok: [localhost]

PLAY [rocky9.osimages.luna] ****************************************************

TASK [trinity/slurm : Install slurm] *******************************************
fatal: [rocky9.osimages.luna]: FAILED! => {"msg": "No package slurm available"}

NO MORE HOSTS LEFT *************************************************************

PLAY RECAP *********************************************************************
rocky9.osimages.luna       : ok=0    changed=0    unreachable=0    failed=1    skipped=0
""".lstrip("\n")


class Ran:
    """
    What a stubbed command answered, in the shape shell.Result has.
    """

    def __init__(self, out: str = "", code: int = 0, err: str = "") -> None:
        """
        Hold what the stubbed command answered.
        """
        self.out = out
        self.err = err
        self.code = code
        self.ok = code == 0


class Commands:
    """
    What the daemon or the unit ran, and what each command answers: the probes as a ready
    controller answers them, and nothing mounted.
    """

    def __init__(self) -> None:
        """
        Nothing run yet; every command succeeds and says nothing.
        """
        self.ran: list = []
        self.answers: dict = {"findmnt": Ran("")}

    def run(self, argv, **kwargs) -> Ran:
        """
        Record the call and answer for the command's base name.
        """
        self.ran.append([str(item) for item in argv])
        answer = self.answers.get(os.path.basename(argv[0]), Ran())
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        return answer

    def of(self, command: str) -> list:
        """
        Every call of one command.
        """
        return [argv for argv in self.ran if os.path.basename(argv[0]) == command]


class Child:
    """
    A book that prints what it is given and ends with the code it is given; the hook
    runs after the second line, which is where a stop arrives mid-play.
    """

    def __init__(self, lines: list, code: int, hook=None) -> None:
        """
        Hold the output and the exit code.
        """
        self.pid = 4242
        self.code = code
        self.ended = False
        self.stdout = self._stream(lines, hook)

    def _stream(self, lines, hook):
        """
        The lines, one at a time.
        """
        for number, line in enumerate(lines):
            if hook is not None and number == 2:
                hook()
            yield line

    def poll(self):
        """
        Still running until it has been waited for.
        """
        return self.code if self.ended else None

    def wait(self) -> int:
        """
        The exit code.
        """
        self.ended = True
        return self.code


@pytest.fixture
def sigterm_restored():
    """
    The runner installs its own SIGTERM handler; the test process gets its own back.
    """
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_the_playbook_is_invoked_as_the_proven_build_was():
    """
    The argument list the prototype proved, every value one argument: the lchroot routing,
    the pack skipped, and the verdict's variables after them, each as
    a one-key JSON document.
    """
    verdict = dict(VERDICT, skip_tags=["monitoring"],
                   variables={"image_el_releasever": "9", "enable_slurm": True})
    assert runner.playbook_argv(REQUEST, verdict, BUILT) == [
        "/trinity/local/python/bin/ansible-playbook", "compute-redhat.yml",
        "-e", "image_name=rocky9",
        "-e", "ansible_remote_tmp=/tmp/.ansible-lpart",
        "--skip-tags", "pack-images,monitoring",
        "-e", "ansible_service_mgr=systemd", "-e", "ansible_become=false",
        "-e", "image_proc_mount=false", "-e", "trix_images=/trinity/images",
        "-e", '{"image_chroot_cmd": "lchroot --path {{ image_path }} --force --"}',
        "-e", '{"image_el_releasever": "9"}',
        "-e", '{"enable_slurm": true}']


def test_a_string_variable_with_a_space_reaches_ansible_in_one_piece():
    """
    ansible splits a key=value extra variable on whitespace, so only a JSON document keeps
    a value such as the debootstrap flags whole.
    """
    flags = "--arch=arm64 --foreign"
    verdict = dict(VERDICT, variables={"image_debootstrap_flags": flags})
    argv = runner.playbook_argv(REQUEST, verdict, BUILT)
    given = {}
    for number, item in enumerate(argv[:-1]):
        if item == "-e" and argv[number + 1].startswith("{"):
            given.update(json.loads(argv[number + 1]))
    assert given.get("image_debootstrap_flags") == flags
    assert not [item for item in argv if item.startswith("image_debootstrap_flags=")]


def test_the_environment_names_the_books_and_the_lchroot_connection():
    """
    The book finds its connection plugin, roles, collections and inventory where the
    settings say.
    """
    inventory = "hosts,/etc/ansible/hosts,dynamic_hosts,/var/job/inventory/hosts.yml"
    env = runner.playbook_env(BUILT, inventory)
    assert env == {"LUNA_IMAGE_CONNECTION": "lchroot",
                   "ANSIBLE_CONNECTION_PLUGINS": "/trinity/trinityx-combined/site/plugins/connection",
                   "ANSIBLE_ROLES_PATH": "/trinity/trinityx-combined/site/roles:~/.ansible/roles"
                                         ":/usr/share/ansible/roles:/etc/ansible/roles",
                   "ANSIBLE_COLLECTIONS_PATH": "/trinity/local/trixd/collections",
                   "ANSIBLE_COLLECTIONS_SCAN_SYS_PATH": "False",
                   "ANSIBLE_HOST_KEY_CHECKING": "False",
                   "ANSIBLE_INVENTORY": inventory,
                   "PYTHONUNBUFFERED": "1"}


def test_the_inventory_names_no_interpreter_for_any_host(tmp_path):
    """
    The run's inventory is a hosts file after the books' own and nothing more: the Python
    of the image and of the controller is ansible's to discover, as in a hand run.
    """
    hosts = runner.write_inventory(str(tmp_path))
    assert hosts == str(tmp_path / "inventory" / "hosts.yml")
    assert (tmp_path / "inventory" / "hosts.yml").read_text() == "all: {}\n"
    assert os.listdir(tmp_path / "inventory") == ["hosts.yml"]
    books = tmp_path / "books"
    books.mkdir()
    (books / "ansible.cfg").write_text("[defaults]\ninventory = hosts,/etc/ansible/hosts,dynamic_hosts\n")
    assert runner.inventory_sources(str(books), hosts) == \
        f"hosts,/etc/ansible/hosts,dynamic_hosts,{hosts}"


def test_a_mounted_disc_is_laid_out_as_the_mirror_the_books_expect(tmp_path):
    """
    The books append <Repo>/<arch>/os to the base URL; a disc keeps BaseOS and AppStream
    at its root, so each is linked under that path. A disc with neither is linked whole.
    """
    disc = tmp_path / "mnt" / "0"
    (disc / "BaseOS").mkdir(parents=True)
    (disc / "AppStream").mkdir()
    repo = tmp_path / "repo" / "0"
    assert mounts.mirror_layout(str(disc), str(repo), "x86_64") == f"file://{repo}/"
    assert os.readlink(repo / "BaseOS" / "x86_64" / "os") == str(disc / "BaseOS")
    assert os.readlink(repo / "AppStream" / "x86_64" / "os") == str(disc / "AppStream")
    assert os.path.isdir(repo / "BaseOS" / "x86_64" / "os")
    # A second run over the same directory replaces the links rather than failing on them.
    assert mounts.mirror_layout(str(disc), str(repo), "x86_64") == f"file://{repo}/"

    other = tmp_path / "mnt" / "1"
    (other / "suse").mkdir(parents=True)
    whole = tmp_path / "repo" / "1"
    assert mounts.mirror_layout(str(other), str(whole), "x86_64") == f"file://{whole}/"
    assert os.readlink(whole) == str(other)


def test_progress_relays_plays_tasks_and_fatals_with_the_run_deciding_their_status():
    """
    One entry per play and task, the padding gone. A fatal that ansible says it ignored is
    not relayed at all, a rescued one is a 200 because the run went on, and only the one
    the run ended on is a 500.
    """
    parser = runner.Progress()
    entries = []
    for line in EXCERPT.splitlines(keepends=True):
        entries.extend(parser.feed(line))
    entries.extend(parser.flush())
    assert entries == [
        ("PLAY [localhost]", 200),
        ("TASK [Set image name]", 200),
        ("PLAY [controllers]", 200),
        ("TASK [trinity/image-create : Bootstrap]", 200),
        ("TASK [luna/osimage-create : Verify if image already exists in luna]", 200),
        ("TASK [trinity/image-create : Try the fast path]", 200),
        ('fatal: [localhost]: FAILED! => {"msg": "no fast path"}', 200),
        ("TASK [trinity/image-create : Rescue]", 200),
        ("PLAY [rocky9.osimages.luna]", 200),
        ("TASK [trinity/slurm : Install slurm]", 200),
        ('fatal: [rocky9.osimages.luna]: FAILED! => {"msg": "No package slurm available"}', 500),
        ("PLAY RECAP", 200)]
    assert parser.fatal == 'fatal: [rocky9.osimages.luna]: FAILED! => {"msg": "No package slurm available"}'

    # The same ignored fatal with the marker on its own line or at the end of it.
    for ignored_line in ('fatal: [localhost]: FAILED! => {"rc": 1}\n...ignoring\n',
                         'fatal: [localhost]: FAILED! => {"rc": 1}...ignoring\n'):
        quiet = runner.Progress()
        assert [entry for line in ignored_line.splitlines(keepends=True) for entry in quiet.feed(line)] == []
        assert quiet.flush() == []

    # Output that stops on a fatal, with nothing after it, ended on it.
    cut = runner.Progress()
    assert cut.feed("fatal: [localhost]: UNREACHABLE! => {}\n") == []
    assert cut.flush() == [("fatal: [localhost]: UNREACHABLE! => {}", 500)]
    assert cut.fatal == "fatal: [localhost]: UNREACHABLE! => {}"

    # An ignored fatal is not blamed for what ends the run after it: the ERROR! line is.
    ignored = runner.Progress()
    for line in IGNORED_END.splitlines(keepends=True):
        ignored.feed(line)
    assert ignored.flush() == [("ERROR! A worker was found in a dead state", 500)]
    assert ignored.fatal == "ERROR! A worker was found in a dead state"


def test_an_error_before_any_play_is_relayed_as_the_fatal_that_ended_the_run():
    """
    ansible says ERROR! when it cannot start (a role it cannot find, a file it cannot
    read): with nothing after it that is the line that ended the run, a 501 once pushed,
    and one followed by the run going on is a 200, as a fatal is.
    """
    cut = runner.Progress()
    assert cut.feed("ERROR! the role 'pcs-modules' was not found in /roles\n") == []
    assert cut.feed("\n") == []
    assert cut.flush() == [("ERROR! the role 'pcs-modules' was not found in /roles", 500)]
    assert cut.fatal == "ERROR! the role 'pcs-modules' was not found in /roles"
    went_on = runner.Progress()
    assert went_on.feed("ERROR! a worker was lost\n") == []
    assert went_on.feed("TASK [next] ***\n") == [("ERROR! a worker was lost", 200), ("TASK [next]", 200)]


# A run that stops on an ERROR! line, after a fatal one it was told to ignore.
IGNORED_END = """
TASK [trinity/image-create : Try the fast path] ********************************
fatal: [localhost]: FAILED! => {"msg": "no fast path"}
...ignoring
ERROR! A worker was found in a dead state
""".lstrip("\n")


def _job(tmp_path, monkeypatch, media=MIRROR, osrelease: str = "9.6", kernels: tuple = (),
         client: str = "2.2-4", tools: bool = True, output: str = EXCERPT, code: int = 0,
         hook=None, commands: Commands = None, mirrors_from: str = None, **record) -> tuple:
    """
    A job as the create writes it, in a temporary state root: the books, the collections
    and the Galaxy role laid out, the tree under the images root, the sources a Rocky 9.8
    mirror root of the scratch tree unless the test names its own, and the tools, the
    probes, the controller's release, the book itself and the mount table all stubbed.
    Answers the job directory, what the fake book was started with, and what was unmounted.
    The record's own words, and where the sources came from, are the request's unless given.
    """
    repo = tmp_path / "repo"
    books = repo / "site"
    (books / "group_vars").mkdir(parents=True)
    (books / "plugins" / "connection").mkdir(parents=True)
    (books / "group_vars" / "all.yml").write_text("trix_version: 15\n")
    (books / "ansible.cfg").write_text("[defaults]\ninventory = hosts,dynamic_hosts\n")
    (books / "plugins" / "connection" / "lchroot.py").write_text("")
    for name in COLLECTIONS:
        (tmp_path / "collections" / "ansible_collections" / name.replace(".", "/")).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(runner, "GALAXY_ROLES", ("~/.ansible/roles",))
    (tmp_path / ".ansible" / "roles" / GALAXY_ROLE).mkdir(parents=True)
    images = tmp_path / "images"
    for version in kernels:
        (images / "rocky9" / "lib" / "modules" / version).mkdir(parents=True)
    if media is MIRROR:
        media = [rocky_root(str(tmp_path / "mirror"), "9.8")]
    settings = dict(SETTINGS, repo=str(repo), images_root=str(images),
                    collections=str(tmp_path / "collections"),
                    answers=str(tmp_path / "backup" / "all.yml"))
    request = dict(REQUEST, path=str(images / "rocky9"), osrelease=osrelease, media=media,
                   mirrors_from=mirrors_from or ("create" if media else None), settings=settings,
                   **record)
    state_root = str(tmp_path / "state")
    jobs.create(state_root, "rocky9", request, {"arch": "x86_64"})
    jobdir = jobs.path(state_root, "rocky9")

    started = []
    unmounted = []
    monkeypatch.setattr(runner.shutil, "which", lambda tool: f"/usr/bin/{tool}" if tools else None)
    monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runner, "client_version", lambda tree: client)
    monkeypatch.setattr(runner, "controller_release", lambda: dict(CONTROLLER), raising=False)
    monkeypatch.setattr(runner.shell, "run", (commands or Commands()).run)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda argv, **kwargs: started.append((argv, kwargs))
                        or Child(output.splitlines(keepends=True), code, hook))
    monkeypatch.setattr(runner.mounts, "unmount_all",
                        lambda path: unmounted.append(path) or [])
    return jobdir, started, unmounted


def _result(jobdir: str) -> dict:
    """
    What the runner left behind.
    """
    assert os.path.isfile(os.path.join(jobdir, jobs.RESULT)), "no result.json was written"
    return jobs.read_json(jobdir, jobs.RESULT)


def test_a_build_that_ends_well_records_the_kernel_and_the_client(tmp_path, monkeypatch, sigterm_restored):
    """
    The real path end to end with the book faked: the sources are read in the unit and the
    verdict written beside the request, the book is started with the proven arguments in
    the books directory, its lines land in the log and the progress file after the
    runner's own, the newest plain kernel and the client version are the contract, and
    the mount directory is read back.
    """
    jobdir, started, unmounted = _job(tmp_path, monkeypatch,
                                      kernels=("5.14.0-503.el9", "5.14.0-503.el9+debug",
                                               "5.14.0-99.el9"))
    assert runner.Build(jobdir).run() == 0
    assert _result(jobdir) == {"ok": True, "exit": 0, "reason": "",
                               "kernelversion": "5.14.0-503.el9", "client_version": "2.2-4",
                               "mounts_left": []}
    assert unmounted == [os.path.join(jobdir, "mnt")]
    verdict = jobs.read_json(jobdir, jobs.VERDICT)
    assert verdict["detected"] == "rocky 9.8 x86_64 from 1 source"
    assert verdict["variables"]["image_el_baseurl"] == f"file://{tmp_path}/mirror/"
    (argv, kwargs), = started
    request = jobs.read_json(jobdir, jobs.REQUEST)
    assert argv == runner.playbook_argv(request, verdict, dict(request["settings"],
                                                               books=str(tmp_path / "repo" / "site")))
    assert argv[1] == "compute-redhat.yml" and "--skip-tags" in argv
    assert kwargs["cwd"] == str(tmp_path / "repo" / "site") and kwargs["start_new_session"] is True
    assert kwargs.get("errors") == "replace", "a byte that is not UTF-8 would end the read loop"
    assert kwargs["env"]["ANSIBLE_INVENTORY"] == f"hosts,dynamic_hosts,{jobdir}/inventory/hosts.yml"
    assert EXCERPT in open(os.path.join(jobdir, "build.log"), encoding="utf-8").read()
    lines, following = jobs.lines_since(jobdir)
    assert [line["message"] for line in lines][:4] == [
        "mirrors: 1 given on the create", "building rocky 9.8 x86_64 from 1 source", "PLAY [localhost]",
        "TASK [Set image name]"]
    assert lines[-1]["message"] == "done" and following == len(lines)
    assert jobs.read_phase(jobdir) == "contract"
    assert oct(os.stat(os.path.join(jobdir, "build.log")).st_mode & 0o777) == "0o600"


def test_the_answers_are_a_file_of_the_inventory_that_nobody_else_reads(tmp_path):
    """
    The answers go in as a file of the group_vars/all directory, which ansible reads where
    there is no group_vars/all.yml; they hold passwords, so the file is the runner's alone.
    """
    runner.write_inventory(str(tmp_path), "trix_version: 15\n")
    where = tmp_path / "inventory" / "group_vars"
    assert not (where / "all.yml").exists()
    assert (where / "all" / "answers.yml").read_text() == "trix_version: 15\n"
    assert oct(os.stat(where / "all" / "answers.yml").st_mode & 0o777) == "0o600"


def test_the_checkouts_own_answers_are_left_to_ansible(tmp_path, monkeypatch, sigterm_restored):
    """
    A checkout that carries site/group_vars/all.yml has it found beside the playbooks, so
    the run's inventory is given no copy, even when the installer's copy differs.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, kernels=("5.14.0-503.el9",))
    backup = tmp_path / "backup.yml"
    backup.write_text("trix_version: other\n")
    request = jobs.read_json(jobdir, jobs.REQUEST)
    request["settings"]["answers"] = str(backup)
    jobs.files.write(os.path.join(jobdir, jobs.REQUEST), json.dumps(request))

    assert runner.Build(jobdir).run() == 0
    assert not os.path.exists(os.path.join(jobdir, "inventory", "group_vars"))
    assert os.path.exists(os.path.join(jobdir, "inventory", "hosts.yml"))


def test_without_answers_in_the_checkout_the_installers_copy_is_the_runs_own(tmp_path, monkeypatch,
                                                                              sigterm_restored):
    """
    The installer's copy of the answers is copied into the run's inventory, readable by
    the runner alone, and the book's command line is the one it is without any answers.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, kernels=("5.14.0-503.el9",))
    os.remove(tmp_path / "repo" / "site" / "group_vars" / "all.yml")
    backup = tmp_path / "backup.yml"
    backup.write_text("trix_version: 15\nroot_password: secret\n")
    request = jobs.read_json(jobdir, jobs.REQUEST)
    request["settings"]["answers"] = str(backup)
    jobs.files.write(os.path.join(jobdir, jobs.REQUEST), json.dumps(request))

    assert runner.Build(jobdir).run() == 0
    copy = os.path.join(jobdir, "inventory", "group_vars", "all", "answers.yml")
    assert os.path.isfile(copy), "the installer's copy was not put in the run's inventory"
    assert open(copy, encoding="utf-8").read() == "trix_version: 15\nroot_password: secret\n"
    assert oct(os.stat(copy).st_mode & 0o777) == "0o600"
    (argv, _), = started
    assert argv == runner.playbook_argv(request, jobs.read_json(jobdir, jobs.VERDICT), dict(
        request["settings"], books=str(tmp_path / "repo" / "site")))


def test_a_run_with_no_answers_anywhere_fails_before_the_book_starts(tmp_path, monkeypatch,
                                                                     sigterm_restored):
    """
    Neither the checkout nor the installer's copy holds the answers: the result says where
    both were looked for, and no book is started to fail on an undefined variable.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, kernels=("5.14.0-503.el9",))
    os.remove(tmp_path / "repo" / "site" / "group_vars" / "all.yml")

    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == (f"no answer file at {tmp_path / 'backup' / 'all.yml'} or in "
                                         f"{tmp_path / 'repo' / 'site'}")
    assert started == []


def test_a_rocky_8_source_is_built_as_any_other_release_is(tmp_path, monkeypatch, sigterm_restored):
    """
    The books' ansible drives the Python 3.6 of a Rocky 8 image by itself: the build names
    no module library, puts no second Python in the image and pins no interpreter.
    """
    eight = rocky_root(str(tmp_path / "mirror8"), "8.10")
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=[eight], osrelease="8.10",
                              kernels=("4.18.0-553.el8",))
    assert runner.Build(jobdir).run() == 0
    (argv, kwargs), = started
    assert "ANSIBLE_LIBRARY" not in kwargs["env"]
    assert not [item for item in argv if "image_bootstrap_extra" in item]
    assert not os.path.exists(os.path.join(jobdir, "inventory", "host_vars"))


@pytest.mark.parametrize("setup, reason", [
    ({"tools": False}, "lchroot and bwrap not on PATH, and without them the books would chroot by hand"),
    ({"code": 2}, 'ansible-playbook exited 2: fatal: [rocky9.osimages.luna]: FAILED! => '
                  '{"msg": "No package slurm available"}'),
    ({"code": 2, "output": IGNORED_END}, "ansible-playbook exited 2: ERROR! A worker was found in a "
                                         "dead state"),
    ({"kernels": ("5.14.0-503.el9+debug",)}, "no plain kernel under {images}/rocky9/lib/modules"),
    ({"kernels": ("5.14.0-503.el9",), "client": ""},
     "the luna2-client package is not installed in the image"),
])
def test_every_way_a_build_fails_leaves_a_result_naming_why(tmp_path, monkeypatch, sigterm_restored,
                                                            setup, reason):
    """
    A missing tool, a book that failed, a tree without a plain kernel, a tree without the
    client: each ends with result.json saying so, the mounts read back, and nothing deleted.
    """
    jobdir, _, unmounted = _job(tmp_path, monkeypatch, **setup)
    assert runner.Build(jobdir).run() == 1
    result = _result(jobdir)
    assert result["ok"] is False and result["exit"] == 1
    assert result["reason"] == reason.replace("{images}", str(tmp_path / "images"))
    assert unmounted == [os.path.join(jobdir, "mnt")]
    if setup.get("kernels"):
        assert os.path.isdir(tmp_path / "images" / "rocky9" / "lib" / "modules")


@pytest.mark.parametrize("missing, said", [
    ("books", "the books are not at {repo}/site: ansible.cfg missing"),
    ("collections", "the collections are not at {collections}: ansible.posix, community.mysql missing"),
    ("role", "the Galaxy role OndrejHome.pcs-modules-2 is not in ~/.ansible/roles"),
    ("playbook", "{python}/bin/ansible-playbook is not installed: the trixd role installs TrinityX's "
                 "ansible under {python}"),
])
def test_a_controller_short_of_what_the_build_needs_ends_the_build_naming_it(tmp_path, monkeypatch,
                                                                             sigterm_restored,
                                                                             missing, said):
    """
    The books, the collections they import, the Galaxy role they name and the
    ansible-playbook under the interpreter are checked before any source is read, so a build never starts to fail an hour in: the result
    names what is missing and no book runs.
    """
    commands = Commands()
    jobdir, started, _ = _job(tmp_path, monkeypatch, commands=commands)
    if missing == "books":
        os.remove(tmp_path / "repo" / "site" / "ansible.cfg")
    elif missing == "collections":
        for name in ("ansible/posix", "community/mysql"):
            os.rmdir(tmp_path / "collections" / "ansible_collections" / name)
    elif missing == "role":
        os.rmdir(tmp_path / ".ansible" / "roles" / GALAXY_ROLE)
    else:
        commands.answers["ansible-playbook"] = BackendFailure(
            f"{SETTINGS['python']}/bin/ansible-playbook is not installed")
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == said.format(repo=tmp_path / "repo", python=SETTINGS["python"],
                                                    collections=tmp_path / "collections")
    assert started == []


def test_the_ansible_under_the_interpreter_is_used_whatever_its_version(tmp_path, monkeypatch,
                                                                        sigterm_restored):
    """
    The ansible of TrinityX's own Python is the one TrinityX ships for its books: the
    build does not judge its version and runs the book with it.
    """
    commands = Commands()
    commands.answers["ansible-playbook"] = Ran("ansible-playbook [core 2.15.13]\n  config file = None")
    jobdir, started, _ = _job(tmp_path, monkeypatch, commands=commands, kernels=("5.14.0-503.el9",))
    assert runner.Build(jobdir).run() == 0
    (argv, _), = started
    assert argv[0] == f"{SETTINGS['python']}/bin/ansible-playbook"


def test_a_build_needs_only_the_collections_and_roles_of_its_familys_playbook(tmp_path, monkeypatch,
                                                                                sigterm_restored):
    """
    Only the redhat setup play names the pcs Galaxy role, and only the redhat and ubuntu
    playbooks load the slurm role whose mysql modules need community.mysql: an Ubuntu
    build on a controller without the role, and a Leap build without the role or that
    collection, still start their book, where the redhat build is refused naming what
    is missing.
    """
    for family, distribution, osrelease, media, absent in (
            ("debian", "ubuntu", "24.04", None, ()),
            ("suse", "opensuse", "15.6", [LEAP_MIRROR], ("community/mysql",)),
            ("redhat", "redhat", "9.6", MIRROR, ())):
        root = tmp_path / family
        if family == "suse":
            jobdir, started, _ = _leap_job(root, monkeypatch, "baseurl={{ opensuse_repo_baseurl }}\n",
                                           kernels=("6.8.0-45",))
        else:
            if family == "debian":
                put(root / "archive", "dists/noble/Release", NOBLE_RELEASE)
                media = [str(root / "archive")]
            jobdir, started, _ = _job(root, monkeypatch, media=media, osrelease=osrelease,
                                      distribution=distribution, kernels=("6.8.0-45",))
        os.rmdir(root / ".ansible" / "roles" / GALAXY_ROLE)
        for name in absent:
            os.rmdir(root / "collections" / "ansible_collections" / name)
        code = runner.Build(jobdir).run()
        if family == "redhat":
            assert code == 1 and started == [], family
            assert _result(jobdir)["reason"] == f"the Galaxy role {GALAXY_ROLE} is not in ~/.ansible/roles"
        else:
            assert code == 0 and started, (family, _result(jobdir)["reason"])


def test_every_family_says_what_its_playbook_needs_on_the_controller():
    """
    The preconditions read the family table, so a family added without its collections
    and Galaxy roles would be checked for nothing.
    """
    for family, rule in rules.FAMILIES.items():
        assert isinstance(rule.get("collections"), tuple) and rule["collections"], family
        assert isinstance(rule.get("galaxy_roles"), tuple), family
    assert rules.FAMILIES["redhat"]["galaxy_roles"] == ("OndrejHome.pcs-modules-2",)
    assert "community.mysql" not in rules.FAMILIES["suse"]["collections"]


def test_an_empty_collections_setting_leaves_the_path_to_ansible():
    """
    The books' collections are wherever ansible looks by itself: the environment names no
    collections path, and the one that is set is still handed on.
    """
    inventory = "hosts,/var/job/inventory/hosts.yml"
    assert "ANSIBLE_COLLECTIONS_PATH" not in runner.playbook_env(dict(BUILT, collections=""), inventory)
    assert runner.playbook_env(dict(BUILT, collections="/srv/collections"), inventory)[
        "ANSIBLE_COLLECTIONS_PATH"] == "/srv/collections"


# What ansible-galaxy answers on the controller the installer's prepare step made: each
# search path with the collections found in it.
GALAXY_LISTED = {
    "/root/.ansible/collections/ansible_collections": {
        "ansible.mysql": {"version": "5.2.0"}, "community.crypto": {"version": "3.5.1"},
        "community.mysql": {"version": "5.0.2"}},
    "/usr/share/ansible/collections/ansible_collections": {
        "ansible.posix": {"version": "2.2.1"}, "community.general": {"version": "10.7.3"}}}


def _on_ansibles_path(jobdir: str) -> None:
    """
    Empty the collections setting of a job, as the default leaves it.
    """
    request = jobs.read_json(jobdir, jobs.REQUEST)
    request["settings"]["collections"] = ""
    jobs.files.write(os.path.join(jobdir, jobs.REQUEST), json.dumps(request))


def test_the_collections_ansible_finds_by_itself_let_the_build_start(tmp_path, monkeypatch, sigterm_restored):
    """
    With the collections setting empty, ansible is asked what it finds on its search path,
    and a controller that has all five the books import goes on to run the book.
    """
    commands = Commands()
    commands.answers["ansible-galaxy"] = Ran(json.dumps(GALAXY_LISTED))
    jobdir, started, _ = _job(tmp_path, monkeypatch, commands=commands, kernels=("5.14.0-503.el9",))
    _on_ansibles_path(jobdir)
    assert runner.Build(jobdir).run() == 0, _result(jobdir)["reason"]
    assert commands.of("ansible-galaxy") == [[f"{SETTINGS['python']}/bin/ansible-galaxy", "collection",
                                              "list", "--format", "json"]]
    assert len(started) == 1


def test_a_collection_ansible_does_not_find_ends_the_build_naming_it(tmp_path, monkeypatch, sigterm_restored):
    """
    The ones that are not in any of the paths ansible lists are named, in the order the
    books import them, and no book runs.
    """
    commands = Commands()
    found = {"/usr/share/ansible/collections/ansible_collections": {
        "community.general": {"version": "10.7.3"}, "community.crypto": {"version": "2.17.1"}}}
    commands.answers["ansible-galaxy"] = Ran(json.dumps(found))
    jobdir, started, _ = _job(tmp_path, monkeypatch, commands=commands)
    _on_ansibles_path(jobdir)
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == ("the collections are not on ansible's search path: "
                                         "ansible.posix, community.mysql missing")
    assert started == []


def test_an_ansible_that_cannot_list_its_collections_ends_the_build_saying_so(tmp_path, monkeypatch,
                                                                              sigterm_restored):
    """
    No usable collections path at all is ansible-galaxy's error, and the build ends with
    its words, not with every collection reported missing.
    """
    said = "ERROR! - None of the provided paths were usable. Please specify a valid path with --collections-path"
    commands = Commands()
    commands.answers["ansible-galaxy"] = Ran("", 1, said)
    jobdir, started, _ = _job(tmp_path, monkeypatch, commands=commands)
    _on_ansibles_path(jobdir)
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == f"{SETTINGS['python']}/bin/ansible-galaxy did not list the collections: {said}"
    assert started == []


def test_a_tree_with_a_mount_below_it_is_not_built(tmp_path, monkeypatch, sigterm_restored):
    """
    The books write into the tree and a leftover mount there is a door onto the host: the
    build ends naming the mount, before any source is read.
    """
    commands = Commands()
    jobdir, started, _ = _job(tmp_path, monkeypatch, commands=commands)
    tree = tmp_path / "images" / "rocky9"
    (tree / "proc").mkdir(parents=True)
    commands.answers["findmnt"] = Ran(f"/\n{os.path.realpath(tree)}/proc")
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == (f"something is mounted below {tree}: "
                                         f"{os.path.realpath(tree)}/proc; unmount it first")
    assert started == [] and "playbook" not in jobs.read_json(jobdir, jobs.VERDICT)


def test_a_source_detection_refuses_ends_the_build_naming_it(tmp_path, monkeypatch, sigterm_restored):
    """
    The sources are read in the unit, after the daemon answered Luna: a source that
    identifies nothing is the reason the build failed, in detection's own words, and no
    book runs.
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=[str(bare)])
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == (f"{bare}: no self-describing metadata found (.treeinfo, "
                                         f".disk/info, dists/*/Release, media.1/products, os-release)")
    assert started == []


def test_the_sources_are_held_against_the_record_and_a_mismatch_ends_the_build(tmp_path, monkeypatch,
                                                                              sigterm_restored):
    """
    A Rocky 8 mirror for a record that says 9.6 is refused naming both, so the record and
    the tree can never disagree; a point release apart from the record's, as the 9.8
    mirror of the other tests is from their 9.6, is not a mismatch.
    """
    eight = rocky_root(str(tmp_path / "mirror8"), "8.10")
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=[eight], osrelease="9.6")
    assert runner.Build(jobdir).run() == 1
    reason = _result(jobdir)["reason"]
    assert reason == ("the sources are rocky 8.10 x86_64 from 1 source, distribution redhat release 8, "
                      "and the record says distribution redhat osrelease 9.6")
    assert started == []


def test_a_job_without_sources_runs_the_book_with_no_source_override(tmp_path, monkeypatch,
                                                                     sigterm_restored):
    """
    No media and no mirrors entry: the compute book of the record's distribution runs with
    the arch's variables and nothing naming a mirror or a release, so the books build from
    what they hold, this controller's own repositories for a RedHat record of the
    controller's own release; the line relayed says so.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=None, osrelease="9.6",
                              kernels=("5.14.0-503.el9",))
    assert runner.Build(jobdir).run() == 0
    (argv, kwargs), = started
    request = jobs.read_json(jobdir, jobs.REQUEST)
    verdict = jobs.read_json(jobdir, jobs.VERDICT)
    assert argv == runner.playbook_argv(request, verdict, dict(request["settings"],
                                                               books=str(tmp_path / "repo" / "site")))
    assert argv[1] == "compute-redhat.yml"
    given = {}
    for number, item in enumerate(argv[:-1]):
        if item == "-e":
            given.update(json.loads(argv[number + 1]) if argv[number + 1].startswith("{")
                         else dict([argv[number + 1].split("=", 1)]))
    assert set(given) == {"image_name", "ansible_remote_tmp", "ansible_service_mgr",
                          "ansible_become", "image_proc_mount", "trix_images", "image_chroot_cmd"}
    assert verdict["variables"] == {} and verdict["base"] is None
    assert kwargs["cwd"] == str(tmp_path / "repo" / "site")
    lines = [line["message"] for line in jobs.lines_since(jobdir)[0]]
    assert lines[:2] == ["no mirrors: this controller's own repositories",
                         "building redhat 9.6 x86_64 from no source, this controller's own repositories"]


def test_a_record_the_books_cannot_place_without_a_source_ends_the_build_naming_it(tmp_path, monkeypatch,
                                                                                   sigterm_restored):
    """
    A RedHat record of another release than this controller's, with no source: the
    books would build the controller's own, so the build ends naming both and what to
    name.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=None, osrelease="8.10")
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == (
        "without a source the books build a redhat image from this controller's own "
        "repositories, which are rocky 9, and the record says osrelease 8.10: name a source in "
        "the create, or a mirrors entry for it in the settings")
    assert started == []


def _said(jobdir: str) -> list:
    """
    The lines the build relayed, in order.
    """
    return [line["message"] for line in jobs.lines_since(jobdir)[0]]


def _books_file(tmp_path, relative: str, text: str) -> str:
    """
    A file of the fake checkout's image-create role, as the books lay it out. Answers the
    books directory.
    """
    books = tmp_path / "repo" / "site"
    path = books / "roles" / "trinity" / "image-create" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(books)


@pytest.mark.parametrize("given, from_where, said", [
    (["a mirror"], "create", "mirrors: 1 given on the create"),
    (["a mirror"], "held", "mirrors: 1 held from luna osimage create by alice"),
    (["a mirror"], "redhat/9.6", "mirrors: from the builder's settings for redhat/9.6"),
    (None, None, "no mirrors: this controller's own repositories"),
])
def test_the_first_line_of_a_build_says_where_its_sources_came_from(tmp_path, monkeypatch,
                                                                    sigterm_restored, given, from_where, said):
    """
    The sources of a create are the body's, else the ones held for its name, else the
    mirrors map's entry for the record, else the books' own; the operator is told which,
    and who held them, before what was built from them.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=MIRROR if given else None,
                              mirrors_from=from_where, kernels=("5.14.0-503.el9",), held_by="alice")
    assert runner.Build(jobdir).run() == 0
    lines = _said(jobdir)
    assert lines[0] == said
    assert lines[1].startswith("building ")


def test_a_redhat_image_for_another_arch_than_the_controllers_needs_a_source(tmp_path, monkeypatch,
                                                                             sigterm_restored):
    """
    The books seed a foreign arch only from a mirror: with none, the build is refused
    before the book runs, naming what to name, instead of failing late inside it.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=None, osrelease="9", architecture="aarch64")
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == (
        "without a source the books build no aarch64 redhat image on this x86_64 controller: name a "
        "source in the create, or a mirrors entry for it in the settings")
    assert started == []


LEAP_MIRROR = "https://mirror.invalid/opensuse/distribution/leap/15.6/repo/oss/"


def _leap_job(tmp_path, monkeypatch, template: str, kernels: tuple = ()) -> tuple:
    """
    A Leap record with a mirror named, the mirror read as detection reads a Leap tree, and
    the books' repository template as given. Answers the job directory, the book's starts
    and the books directory.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=[LEAP_MIRROR], osrelease="15.6",
                              distribution="opensuse", kernels=kernels)
    mirror = detect.Finding(source=LEAP_MIRROR, kind="repository", family="suse", distro="opensuse-leap",
                            release="15", dotrelease="15.6", arch="x86_64")
    monkeypatch.setattr(detect, "identify_all", lambda media, want_arch=None, release=None: [mirror])
    return jobdir, started, _books_file(tmp_path, "templates/opensuse-repo.j2", template)


def test_a_leap_mirror_is_refused_by_books_that_would_ignore_it(tmp_path, monkeypatch, sigterm_restored):
    """
    The repository template of older books names download.opensuse.org whatever the
    mirror says: the build is refused naming the books, not run to build the wrong tree.
    """
    jobdir, started, books = _leap_job(tmp_path, monkeypatch,
                                       "baseurl=https://download.opensuse.org/distribution/leap/15.6/\n")
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == (f"the books at {books} do not read opensuse_repo_baseurl yet: a "
                                         f"Leap mirror would be ignored; build without mirrors or update the books")
    assert started == []


def test_a_leap_mirror_is_built_by_books_that_read_it(tmp_path, monkeypatch, sigterm_restored):
    """
    Books whose template reads opensuse_repo_baseurl are given the mirror.
    """
    jobdir, started, books = _leap_job(tmp_path, monkeypatch, "baseurl={{ opensuse_repo_baseurl }}\n",
                                       kernels=("5.14.21-150600",))
    assert runner.Build(jobdir).run() == 0
    assert jobs.read_json(jobdir, jobs.VERDICT)["variables"]["opensuse_repo_baseurl"] == LEAP_MIRROR
    assert started


UBUNTU_RELEASES = 'ubuntu_releases:\n  "22": jammy\n  "24": noble\n'


def test_an_ubuntu_release_the_books_do_not_list_is_refused_without_a_source(tmp_path, monkeypatch,
                                                                             sigterm_restored):
    """
    With no source the books map the release to a codename and fall back to noble for one
    they do not list: a record for release 20 is refused naming the releases they do
    list, not built as a noble image labelled 20.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=None, osrelease="20.04", distribution="ubuntu")
    _books_file(tmp_path, "defaults/main.yml", UBUNTU_RELEASES)
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == (
        "without a source the books build ubuntu 22, 24 only, and the record says osrelease 20.04: name a "
        "source in the create, or a mirrors entry for it in the settings")
    assert started == []


def test_an_ubuntu_release_the_books_list_is_built_without_a_source(tmp_path, monkeypatch, sigterm_restored):
    """
    A listed release is placed as it was.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=None, osrelease="24.04", distribution="ubuntu",
                              kernels=("6.8.0-31-generic",))
    _books_file(tmp_path, "defaults/main.yml", UBUNTU_RELEASES)
    assert runner.Build(jobdir).run() == 0
    assert jobs.read_json(jobdir, jobs.VERDICT)["variables"]["ubuntu_version"] == "24"


def _create_job(tmp_path, monkeypatch, builder: str, kernels: tuple = ()) -> tuple:
    """
    A job as Luna's create writes it, its tree under a directory of Luna's own holding
    the kernels given, the books absent and not fetchable, the book itself refusing to
    start, and sleep recorded rather than slept. Answers the job directory, the tree and
    the sleeps.
    """
    tree = tmp_path / "luna" / "rocky9"
    for version in kernels:
        (tree / "lib" / "modules" / version).mkdir(parents=True)
    settings = dict(SETTINGS, repo=str(tmp_path / "nowhere"), images_root=str(tmp_path / "images"),
                    builder=builder, stub_gap_seconds=7)
    state_root = str(tmp_path / "state")
    jobs.create(state_root, "rocky9", dict(REQUEST, path=str(tree), settings=settings), {"arch": "x86_64"})
    slept = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    def refused(*args, **kwargs):
        raise AssertionError("a stub build started a process")
    monkeypatch.setattr(runner.subprocess, "Popen", refused)
    monkeypatch.setattr(runner.shell, "run", refused)
    monkeypatch.setattr(runner.mounts, "unmount_all", lambda path: [])
    return jobs.path(state_root, "rocky9"), tree, slept


def _listing(tree) -> list:
    """
    Every path below a tree, so a build can be shown to have left it alone.
    """
    return sorted(os.path.join(folder, name) for folder, dirs, files in os.walk(tree)
                  for name in dirs + files)


def test_the_stub_builder_says_its_lines_a_gap_apart_and_records_the_trees_kernel(tmp_path, monkeypatch,
                                                                               sigterm_restored):
    """
    No book, no fetch of the books and nothing written into the tree: two lines a
    stub_gap_seconds apart, then the newest plain kernel of Luna's tree, result.json
    saying ok with it, and exit 0, the shape the supervisor and the pusher read.
    """
    jobdir, tree, slept = _create_job(tmp_path, monkeypatch, "stub",
                                      kernels=("5.14.0-503.el9", "5.14.0-503.el9+debug", "5.14.0-99.el9"))
    before = _listing(tree)
    assert runner.Build(jobdir).run() == 0
    assert _result(jobdir) == {"ok": True, "exit": 0, "reason": "", "kernelversion": "5.14.0-503.el9",
                               "client_version": "", "mounts_left": []}
    assert _listing(tree) == before and not os.path.exists(tmp_path / "images")
    assert slept == [7, 7]
    lines, _ = jobs.lines_since(jobdir)
    assert [line["message"] for line in lines] == [
        f"stub builder: no book runs and {tree} is left as it is",
        "stub builder: waiting 7 s as a build would",
        f"kernel 5.14.0-503.el9 from {tree}/lib/modules", "done"]
    assert all(line["status"] == 200 for line in lines)
    assert jobs.read_phase(jobdir) == "contract"


def test_the_stub_builder_reports_a_placeholder_kernel_for_a_tree_that_holds_none(tmp_path, monkeypatch,
                                                                                sigterm_restored):
    """
    A tree that is not there is not made, and the kernel version is a placeholder said
    to be one, so the pack has a version to set and nobody takes it for a real kernel.
    """
    jobdir, tree, slept = _create_job(tmp_path, monkeypatch, "stub")
    assert runner.Build(jobdir).run() == 0
    result = _result(jobdir)
    assert result["ok"] is True and result["kernelversion"] == "0.0.0-stub"
    assert not os.path.exists(tree)
    lines, _ = jobs.lines_since(jobdir)
    assert f"kernel 0.0.0-stub, a placeholder: {tree}/lib/modules holds none" in [
        line["message"] for line in lines]


def test_the_failing_stub_ends_as_a_failed_book_does(tmp_path, monkeypatch, sigterm_restored):
    """
    stub-fail says its lines and then fails where a book's failure is reported: result.json
    with ok false, exit 1 and a reason, which the pusher turns into the failure line and EOF.
    """
    jobdir, tree, slept = _create_job(tmp_path, monkeypatch, "stub-fail")
    assert runner.Build(jobdir).run() == 1
    result = _result(jobdir)
    assert (result["ok"], result["exit"], result["kernelversion"]) == (False, 1, "")
    assert result["reason"] == "the stub builder failed, as builder stub-fail asks"
    assert slept == [7, 7] and not os.path.exists(tree)
    lines, _ = jobs.lines_since(jobdir)
    assert [line["message"] for line in lines][-1] == result["reason"]


def test_a_runner_that_crashed_still_leaves_a_result(tmp_path, monkeypatch, sigterm_restored):
    """
    An error nobody foresaw is a result with the error in it and the traceback in the
    log, not a unit that ended with nothing to read.
    """
    jobdir, _, unmounted = _job(tmp_path, monkeypatch)

    def broken(tool):
        raise RuntimeError("no PATH at all")
    monkeypatch.setattr(runner.shutil, "which", broken)
    assert runner.Build(jobdir).run() == 1
    assert _result(jobdir)["reason"] == "runner error: RuntimeError('no PATH at all')"
    assert "RuntimeError: no PATH at all" in open(os.path.join(jobdir, "build.log")).read()
    assert unmounted == [os.path.join(jobdir, "mnt")]


def test_a_stopped_unit_still_leaves_a_result_and_the_mounts_read_back(tmp_path, monkeypatch,
                                                                        sigterm_restored):
    """
    SIGTERM from the unit being stopped reaches the book's process group, the lines
    already read are kept, and the result says cancelled with what the mount table still
    showed after the unmount.
    """
    signalled = []
    monkeypatch.setattr(runner, "_terminate", lambda child: signalled.append(child.pid))
    jobdir, _, _ = _job(tmp_path, monkeypatch, code=143,
                        hook=lambda: os.kill(os.getpid(), signal.SIGTERM))
    monkeypatch.setattr(runner.mounts, "unmount_all",
                        lambda path: [os.path.join(path, "0")])
    assert runner.Build(jobdir).run() == 143
    assert _result(jobdir) == {"ok": False, "exit": 143, "reason": "cancelled",
                               "kernelversion": "", "client_version": "",
                               "mounts_left": [os.path.join(jobdir, "mnt", "0")]}
    assert signalled == [4242]
    # The handler wrote to the job's files, so it is gone once they are closed.
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    lines, _ = jobs.lines_since(jobdir)
    assert "cancelled, stopping the playbook" in [line["message"] for line in lines]
    assert "still mounted" in open(os.path.join(jobdir, "build.log")).read()


def test_the_book_runs_in_its_own_session_and_its_group_is_noted_for_the_daemon(tmp_path):
    """
    A real book process: it runs in a session of its own, so what the runner forwards
    reaches it and nothing else, and its process group is noted in the job directory
    while it runs, for the daemon's kill of a book that ignores SIGTERM; the note reads
    0 once it ended.
    """
    note = str(tmp_path / "book-group")
    build = runner.Build(str(tmp_path))
    build.log = runner._append(str(tmp_path / "build.log"))
    build.progress = runner._append(str(tmp_path / "progress.jsonl"))
    # The note is written once the book has started, so the book gives it a moment, as the
    # daemon does: it reads the note only after the grace.
    book = [sys.executable, "-c",
            f"import os, time; [time.sleep(0.1) for _ in range(50) if not os.path.exists({note!r})]; "
            f"noted = open({note!r}).read().strip() if os.path.exists({note!r}) else 'none'; "
            f"print(os.getpid(), noted, os.getpgid(0), os.getsid(0) != os.getsid(os.getppid()))"]
    code = build._play(book, dict(os.environ), str(tmp_path))
    build.log.close()
    build.progress.close()
    assert code == 0
    said = open(tmp_path / "build.log", encoding="utf-8").read().splitlines()[-1].split()
    pid, noted, pgid, own_session = said
    assert noted == pid, "the book's process group was not noted while it ran"
    assert pgid == pid and own_session == "True"
    assert open(note, encoding="utf-8").read().strip() == "0"


def test_a_checkout_without_a_hosts_file_gets_this_controller_as_the_controllers_group(tmp_path, monkeypatch):
    """
    A downloaded checkout has no installer-written hosts file, so the controllers play of the
    compute playbook would match nothing and never make the image directory.
    """
    monkeypatch.setattr(runner.socket, "gethostname", lambda: "ctl1.cluster")
    books = tmp_path / "books"
    books.mkdir()
    assert runner.controller_name(str(books)) == "ctl1"
    (books / "hosts").write_text("[controllers]\nctl1\n")
    assert runner.controller_name(str(books)) == ""
    hosts = runner.write_inventory(str(tmp_path / "job"), "", "ctl1")
    import yaml
    group = yaml.safe_load(open(hosts))["all"]["children"]["controllers"]["hosts"]
    assert group == {"ctl1": {"ansible_host": "127.0.0.1", "ansible_connection": "local"}}


def test_a_build_from_a_checkout_without_hosts_runs_with_this_controller_as_controllers(tmp_path, monkeypatch, sigterm_restored):
    """
    The same through a real run: the books of a download have no hosts file, so the run's
    inventory is what makes the controllers play of the compute playbook match a host.
    """
    jobdir, started, _ = _job(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.socket, "gethostname", lambda: "ctl1.cluster")
    runner.Build(jobdir).run()
    assert started, "the book was not started"
    with open(os.path.join(jobdir, "inventory", "hosts.yml")) as handle:
        assert "controllers" in handle.read()


def test_a_stop_during_the_preparation_starts_no_book(tmp_path, monkeypatch, sigterm_restored):
    """
    SIGTERM while the mounts and the inventory are being made: the book is never
    started, and the result still says cancelled rather than the unit being killed
    with nothing written.
    """
    jobdir, started, unmounted = _job(tmp_path, monkeypatch)
    writing = runner.write_inventory

    def stopped_while_writing(*args):
        os.kill(os.getpid(), signal.SIGTERM)
        return writing(*args)
    monkeypatch.setattr(runner, "write_inventory", stopped_while_writing)
    assert runner.Build(jobdir).run() == 143
    assert started == [], "the book was started after the stop"
    assert _result(jobdir)["reason"] == "cancelled"
    assert unmounted == [os.path.join(jobdir, "mnt")]


def test_a_reader_that_broke_stops_the_book_before_the_unmount(tmp_path, monkeypatch,
                                                                sigterm_restored):
    """
    The read loop ending before the book has (here on an error it raised) must not
    leave the book writing into the tree while the mounts go: it is told to stop
    first, and the result says what broke.
    """
    order = []
    monkeypatch.setattr(runner, "_terminate", lambda child: order.append(("terminate", child.pid)))

    def broken():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    jobdir, _, _ = _job(tmp_path, monkeypatch, hook=broken)
    monkeypatch.setattr(runner.mounts, "unmount_all", lambda path: order.append(("unmount", path)) or [])
    assert runner.Build(jobdir).run() == 1
    assert order == [("terminate", 4242), ("unmount", os.path.join(jobdir, "mnt"))]
    assert _result(jobdir)["reason"].startswith("runner error: UnicodeDecodeError(")


def test_the_runner_never_deletes():
    """
    A tree that failed is Luna's to remove, after its own mount check; the runner has no
    delete in it at all.
    """
    source = open(runner.__file__, encoding="utf-8").read()
    for word in ("rmtree", "os.remove", "unlink", "rm -rf"):
        assert word not in source, f"the runner must not delete anything: {word}"


def test_targets_below_sees_a_nested_mount_and_answers_deepest_first(tmp_path, monkeypatch):
    """
    The whole table compared by real-path prefix: a mount two levels down is found, a
    sibling whose name merely starts the same is not, and the deepest comes first so
    unmounting in that order works.
    """
    root = os.path.realpath(tmp_path)
    table = "\n".join(["/", "/proc", f"{root}/job/mnt/0", f"{root}/job/mnt/0/sub",
                       f"{root}/job-other", f"{root}/job/mnt/1", "/trinity/images/x/proc"])
    monkeypatch.setattr(mounts.shell, "run", lambda argv, **kwargs: Ran(table))
    assert mounts.targets_below(str(tmp_path / "job")) == \
        [f"{root}/job/mnt/0/sub", f"{root}/job/mnt/0", f"{root}/job/mnt/1"]
    assert mounts.targets_below(str(tmp_path / "nothing")) == []


def test_unmount_answers_what_the_table_still_shows(tmp_path, monkeypatch):
    """
    Every target is given to umount deepest first and the answer is the table read back,
    not the umount's word for it.
    """
    root = os.path.realpath(tmp_path)
    calls = []
    tables = [f"{root}/job/mnt/0/sub\n{root}/job/mnt/0", f"{root}/job/mnt/0"]

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "findmnt":
            return Ran(tables.pop(0))
        return Ran("", 32, "target is busy") if argv[-1].endswith("/0") else Ran()
    monkeypatch.setattr(mounts.shell, "run", run)
    assert mounts.unmount_all(str(tmp_path / "job")) == [f"{root}/job/mnt/0"]
    assert calls[1:3] == [["umount", "--", f"{root}/job/mnt/0/sub"],
                          ["umount", "--", f"{root}/job/mnt/0"]]


def test_a_job_moves_only_along_the_edges_of_the_machine(tmp_path):
    """
    prepared to running to one of done, failed or cancelled, each with its timestamp;
    any other step is refused and changes nothing.
    """
    root = str(tmp_path)
    state = jobs.create(root, "img", REQUEST, VERDICT)
    assert state["state"] == "prepared" and state["started"] is None
    assert jobs.read_json(jobs.path(root, "img"), jobs.REQUEST) == REQUEST
    with pytest.raises(Conflict, match="is prepared, not done"):
        jobs.move(jobs.path(root, "img"), "done")
    running = jobs.move(jobs.path(root, "img"), "running", unit="trixd-osimage-img")
    assert running["state"] == "running" and running["started"]
    assert running["unit"] == "trixd-osimage-img"
    with pytest.raises(Conflict, match="a build named img is running"):
        jobs.create(root, "img", REQUEST, VERDICT)
    failed = jobs.move(jobs.path(root, "img"), "failed", "interrupted", exit=1)
    assert failed["reason"] == "interrupted" and failed["finished"] and failed["exit"] == 1
    with pytest.raises(Conflict, match="is failed, not cancelled"):
        jobs.move(jobs.path(root, "img"), "cancelled")
    assert jobs.read_state(jobs.path(root, "img"))["state"] == "failed"
    assert jobs.running(root) == []


def test_lines_are_served_from_a_sequence_number_on(tmp_path):
    """
    A reader asks from the number it was given last and gets the lines from there, with
    a line still being written left for the next read; the answer carries what the
    runner recorded once it ended.
    """
    root = str(tmp_path)
    jobs.create(root, "img", REQUEST, VERDICT)
    jobdir = jobs.path(root, "img")
    assert jobs.lines_since(jobdir) == ([], 0)
    with open(os.path.join(jobdir, jobs.PROGRESS), "w", encoding="utf-8") as handle:
        for seq, message in enumerate(["PLAY [localhost]", "TASK [one]", "TASK [two]"]):
            handle.write(json.dumps({"seq": seq, "message": message, "status": 200}) + "\n")
        handle.write('{"seq": 3, "message": "TASK [thr')
    lines, following = jobs.lines_since(jobdir)
    assert [line["seq"] for line in lines] == [0, 1, 2] and following == 3
    lines, following = jobs.lines_since(jobdir, 2)
    assert [line["message"] for line in lines] == ["TASK [two]"] and following == 3
    assert jobs.lines_since(jobdir, 3) == ([], 3)

    jobs.move(jobdir, "running")
    jobs.write_phase(jobdir, "controllers")
    jobs.files.write(os.path.join(jobdir, jobs.RESULT),
                     json.dumps({"ok": True, "kernelversion": "5.14.0", "client_version": "2.2"}))
    shown = jobs.show(jobdir, 1)
    assert shown["state"] == "running" and shown["phase"] == "controllers"
    assert [line["seq"] for line in shown["lines"]] == [1, 2] and shown["next"] == 3
    assert shown["kernelversion"] == "5.14.0" and shown["client_version"] == "2.2"


def test_a_job_only_prepared_is_not_running_and_counts_for_nothing(tmp_path):
    """
    The cap is on builds with a unit: any number may be prepared, and only a job that was
    started is counted as running.
    """
    root = str(tmp_path)
    for name in ("one", "two", "three"):
        jobs.create(root, name, REQUEST, VERDICT)
    assert jobs.running(root) == []
    jobs.move(jobs.path(root, "two"), "running", unit="trixd-osimage-two")
    assert [state["name"] for state in jobs.running(root)] == ["two"]


def test_prune_forgets_the_stale_and_leaves_the_mounted_alone(tmp_path, monkeypatch):
    """
    A job prepared an hour ago and never started, or ended longer ago than kept, is
    removed; a running one, a recent one and one with something still mounted below it
    are not, the last said loudly.
    """
    root = str(tmp_path)
    for name in ("stale", "old", "recent", "busy", "live"):
        jobs.create(root, name, REQUEST, VERDICT)
    for name in ("old", "recent", "busy", "live"):
        jobs.move(jobs.path(root, name), "running")
    for name in ("old", "recent", "busy"):
        jobs.move(jobs.path(root, name), "done")
    ages = {"stale": ("prepared", 2 * 3600), "old": ("finished", 20 * 86400),
            "busy": ("finished", 20 * 86400), "recent": ("finished", 86400)}
    for name, (field, seconds) in ages.items():
        state = jobs.read_state(jobs.path(root, name))
        state[field] = _ago(seconds)
        jobs.files.write(os.path.join(jobs.path(root, name), jobs.STATE), json.dumps(state))
    monkeypatch.setattr(jobs.mounts, "targets_below",
                        lambda path: [path + "/mnt/0"] if path.endswith("/busy") else [])

    assert jobs.prune(root, 14) == ["old", "stale"]
    assert jobs.names(root) == ["busy", "live", "recent"]
    assert jobs.names(str(tmp_path / "absent")) == []


def test_a_build_that_ended_is_replaced_by_the_next_prepare_unless_mounted(tmp_path, monkeypatch):
    """
    A done, failed or cancelled job of the name gives way to a new prepare, its old
    lines and result gone with its directory, and so does one prepared and never
    started; one with a mount still below it is refused naming that, a running one is
    refused saying so, and a prepared one is cancellable without a unit.
    """
    root = str(tmp_path)
    jobs.create(root, "img", REQUEST, VERDICT)
    jobdir = jobs.path(root, "img")
    jobs.move(jobdir, "running")
    jobs.move(jobdir, "failed", "interrupted", exit=1)
    jobs.files.write(os.path.join(jobdir, jobs.RESULT), json.dumps({"ok": False}))
    mounted = []
    monkeypatch.setattr(jobs.mounts, "targets_below", lambda path: list(mounted))

    mounted.append(os.path.join(jobdir, "mnt", "0"))
    with pytest.raises(Conflict, match="a build named img is failed and still has a mount"):
        jobs.create(root, "img", REQUEST, VERDICT)
    assert jobs.read_state(jobdir)["state"] == "failed"

    mounted.clear()
    assert jobs.create(root, "img", REQUEST, VERDICT)["state"] == "prepared"
    assert not os.path.exists(os.path.join(jobdir, jobs.RESULT))
    assert jobs.move(jobdir, "cancelled", "cancelled by tester")["state"] == "cancelled"
    assert jobs.create(root, "img", REQUEST, VERDICT)["state"] == "prepared"
    try:
        replaced = jobs.create(root, "img", REQUEST, VERDICT)["state"]
    except Conflict as refused:
        replaced = str(refused)
    assert replaced == "prepared", "a prepared job nobody started held its name"
    jobs.move(jobdir, "running")
    with pytest.raises(Conflict, match="a build named img is running"):
        jobs.create(root, "img", REQUEST, VERDICT)


def test_a_build_still_being_told_to_luna_keeps_its_name_until_it_is_closed(tmp_path, monkeypatch):
    """
    A job that ended with a request to answer and has not finished its stream is not
    deleted by the next prepare of the name, whatever state it ended in: its lines and
    its pack are still to be delivered. Once the stream is closed the name is free.
    """
    root = str(tmp_path)
    monkeypatch.setattr(jobs.mounts, "targets_below", lambda path: [])
    for state in ("done", "failed", "cancelled"):
        jobs.create(root, "img", REQUEST, VERDICT)
        jobdir = jobs.path(root, "img")
        jobs.move(jobdir, "running", unit="trixd-osimage-img", request_id="1761818733.1")
        jobs.move(jobdir, state)
        with pytest.raises(Conflict, match="the earlier build of img is .* its stream is still being "
                                           "delivered"):
            jobs.create(root, "img", REQUEST, VERDICT)
        assert jobs.read_state(jobdir)["state"] == state
        jobs.update(jobdir, closing="closed")
        assert jobs.create(root, "img", REQUEST, VERDICT)["state"] == "prepared"


def test_a_job_that_already_ended_is_not_finished_twice_and_answers_what_is_recorded(tmp_path, monkeypatch):
    """
    A cancel and the supervisor can meet on one job: whichever is second finds it ended and
    gets what was recorded, not a conflict; a step the machine has no edge for from a state
    that is not an end is still refused.
    """
    root = str(tmp_path)
    monkeypatch.setattr(jobs.mounts, "release", lambda path: [])
    jobs.create(root, "img", REQUEST, VERDICT)
    jobdir = jobs.path(root, "img")
    jobs.move(jobdir, "running", unit="trixd-osimage-img")
    first = jobs.finish(jobdir, "failed", "exceeded 21600 s")
    again = jobs.finish(jobdir, "cancelled", "cancelled by tester")
    assert (again["state"], again["reason"], again["finished"]) == (
        "failed", "exceeded 21600 s", first["finished"])
    jobs.create(root, "other", REQUEST, VERDICT)
    with pytest.raises(Conflict, match="is prepared, not done"):
        jobs.finish(jobs.path(root, "other"), "done")


def test_finishing_a_job_records_the_result_and_lets_go_of_what_was_left_mounted(tmp_path, monkeypatch):
    """
    What the runner recorded decides the exit, what it left below mnt is lazily
    unmounted and what is still there afterwards is said; a mount table that cannot be
    read is said too and does not keep the job running.
    """
    root = str(tmp_path)
    calls, tables = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "findmnt":
            return Ran(tables.pop(0))
        return Ran()
    monkeypatch.setattr(mounts.shell, "run", run)
    jobs.create(root, "img", REQUEST, VERDICT)
    jobdir = jobs.path(root, "img")
    jobs.move(jobdir, "running", unit="trixd-osimage-img")
    jobs.files.write(os.path.join(jobdir, jobs.RESULT),
                     json.dumps({"ok": False, "exit": 143, "reason": "cancelled"}))
    left = os.path.join(os.path.realpath(jobdir), "mnt", "0")
    tables += [left, ""]
    state = jobs.finish(jobdir, "cancelled")
    assert (state["state"], state["exit"], state["reason"]) == ("cancelled", 143, "cancelled")
    assert calls[1] == ["umount", "-l", "--", left]

    jobs.create(root, "other", REQUEST, VERDICT)
    other = jobs.path(root, "other")
    jobs.move(other, "running", unit="trixd-osimage-other")

    def unreadable(argv, **kwargs):
        raise BackendFailure("findmnt is not installed")
    monkeypatch.setattr(mounts.shell, "run", unreadable)
    state = jobs.finish(other, "failed", "not followed for 900 s")
    assert (state["state"], state["exit"], state["reason"]) == ("failed", None, "not followed for 900 s")


def _ago(seconds: int) -> str:
    """
    A timestamp of state.json that long ago.
    """
    then = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return then.isoformat(timespec="seconds")


def _repository(root, name: str, repodata: bool = True) -> str:
    """
    A directory of the scratch tree that is a dnf repository, as reposync lays one out.
    """
    path = root / name
    (path / "Packages").mkdir(parents=True)
    if repodata:
        (path / "repodata").mkdir()
        (path / "repodata" / "repomd.xml").write_text("<repomd/>")
    return str(path)


BASEOS = "rhel-9-for-x86_64-baseos-rpms"
APPSTREAM = "rhel-9-for-x86_64-appstream-rpms"


def _build_of_a_list(tmp_path, monkeypatch, media: list, osrelease: str = "9", books: bool = True,
                     commands: Commands = None, **kwargs) -> tuple:
    """
    One build of a job whose sources are these, with the books that take a repository list
    unless told otherwise: the exit code, the result, the relayed lines, what the fake book
    was started with, the stubbed commands and the job directory.
    """
    commands = commands or Commands()
    jobdir, started, _ = _job(tmp_path, monkeypatch, media=media, osrelease=osrelease,
                              kernels=("5.14.0-503.el9",), commands=commands, **kwargs)
    seed = tmp_path / "repo" / "site" / "roles" / "trinity" / "image-create" / "tasks" / "redhat"
    seed.mkdir(parents=True)
    (seed / "base.yml").write_text("- when: image_el_repos is defined\n" if books else "- when: no\n")
    code = runner.Build(jobdir).run()
    lines, _ = jobs.lines_since(jobdir)
    return code, _result(jobdir), [line["message"] for line in lines], started, commands, jobdir


def _variables(started: list) -> dict:
    """
    The variables the book was started with, each one a JSON document of its own.
    """
    (argv, _), = started
    found = {}
    for flag, value in zip(argv, argv[1:]):
        if flag == "-e" and value.startswith("{"):
            found.update(json.loads(value))
    return found


def test_a_repository_list_build_binds_the_directories_in_the_image_and_lets_go_of_them(
        tmp_path, monkeypatch, sigterm_restored):
    """
    The books are handed the list and the release, the directories of this controller are
    bound read-only under the image where its package manager reads them, each target is
    written to the job before it is mounted, and when the build is over they are unmounted
    and the directories made for them are gone; the image's own /mnt stays.
    """
    media = [_repository(tmp_path, BASEOS), _repository(tmp_path, APPSTREAM)]
    code, result, messages, started, commands, jobdir = _build_of_a_list(tmp_path, monkeypatch, media)
    tree = os.path.realpath(tmp_path / "images" / "rocky9")
    place = f"{tree}/mnt/osimage-repos"

    assert code == 0, result["reason"]
    assert [argv[2:] for argv in commands.of("mount")] == [
        ["-o", "ro", "--", media[0], f"{place}/{BASEOS}"], ["-o", "ro", "--", media[1], f"{place}/{APPSTREAM}"]]
    assert open(os.path.join(jobdir, "tree-mounts")).read().split() == [f"{place}/{BASEOS}", f"{place}/{APPSTREAM}"]
    assert [argv[-1] for argv in commands.of("umount")] == [f"{place}/{APPSTREAM}", f"{place}/{BASEOS}"]
    assert result["mounts_left"] == []
    assert not os.path.exists(place) and os.path.isdir(f"{tree}/mnt")
    variables = _variables(started)
    assert variables["image_el_releasever"] == "9"
    assert variables["image_el_release_packages"] == ["system-release", "python3"]
    assert variables["image_el_repos"] == [
        {"id": BASEOS, "baseurl": f"file://{media[0]}", "image_baseurl": f"file:///mnt/osimage-repos/{BASEOS}"},
        {"id": APPSTREAM, "baseurl": f"file://{media[1]}",
         "image_baseurl": f"file:///mnt/osimage-repos/{APPSTREAM}"}]
    assert "image_el_baseurl" not in variables
    assert messages[0] == f"mirrors: 2 repositories given on the create: {BASEOS}, {APPSTREAM}"
    assert messages[1] == "building redhat 9 x86_64 from 2 repositories"
    assert f"bound {media[0]} read-only in the image at /mnt/osimage-repos/{BASEOS}" in messages


def test_a_repository_list_from_the_settings_is_named_as_such_on_the_rung_line(
        tmp_path, monkeypatch, sigterm_restored):
    """
    The line that opens a build names where the list came from and what is in it.
    """
    media = [_repository(tmp_path, BASEOS)]
    code, result, messages, _, _, _ = _build_of_a_list(tmp_path, monkeypatch, media, mirrors_from="redhat/9")
    assert code == 0, result["reason"]
    assert messages[0] == f"mirrors: from the builder's settings for redhat/9: 1 repositories: {BASEOS}"


def test_a_repository_id_is_its_last_path_segment_made_safe_short_and_unique(
        tmp_path, monkeypatch, sigterm_restored):
    """
    The id dnf knows a repository by and the name of its file in the image: letters, digits,
    dot, dash and underscore, at most 64 characters, and never one already taken.
    """
    media = [_repository(tmp_path / "a", "crb"), _repository(tmp_path / "b", "crb"),
             _repository(tmp_path, "my repo (new)"), _repository(tmp_path, "x" * 70),
             _repository(tmp_path / "c", "x" * 64)]
    code, result, _, started, _, _ = _build_of_a_list(tmp_path, monkeypatch, media)
    assert code == 0, result["reason"]
    assert [repository["id"] for repository in _variables(started)["image_el_repos"]] == [
        "crb", "crb-2", "my-repo-new", "x" * 64, "x" * 62 + "-2"]


@pytest.mark.parametrize("build, said", [
    (lambda root: [_repository(root, BASEOS), _repository(root, APPSTREAM, repodata=False)],
     lambda root: f"media item 2 is not a package repository, there is no repodata/repomd.xml there: "
                  f"{root / APPSTREAM}"),
    (lambda root: [_repository(root, BASEOS), str(root / "gone")],
     lambda root: None),
    (lambda root: [_repository(root, "rhel-10-for-x86_64-baseos-rpms")],
     lambda root: "media item 1 is rhel-10-for-x86_64-baseos-rpms, a release 10 repository, and the "
                  "record says release 9"),
    (lambda root: [_repository(root, "rhel-9-for-aarch64-baseos-rpms")],
     lambda root: "media item 1 is rhel-9-for-aarch64-baseos-rpms, an aarch64 repository, and the "
                  "record says x86_64"),
    (lambda root: [(lambda path: [path, path + "/"])(_repository(root, BASEOS))][0],
     lambda root: f"media item 2 repeats item 1: {root / BASEOS}/"),
])
def test_a_list_that_is_not_what_it_says_is_refused_by_the_position_of_the_item(
        tmp_path, monkeypatch, sigterm_restored, build, said):
    """
    The record is trusted and the list is held to what is there: an item with no repodata or
    no directory, a name that says another release or arch than the record, and the same
    repository twice are each one sentence naming the position, and nothing is mounted.
    """
    media = build(tmp_path)
    expected = said(tmp_path)
    code, result, _, started, commands, _ = _build_of_a_list(tmp_path, monkeypatch, media)
    assert code == 1
    if expected is None:
        assert result["reason"].startswith("media item 2 is not a package repository, ")
        assert result["reason"].endswith(str(tmp_path / "gone"))
    else:
        assert result["reason"] == expected
    assert started == [] and commands.of("mount") == []


def test_a_repository_beside_a_mirror_root_is_still_the_extra_one_the_books_refuse(
        tmp_path, monkeypatch, sigterm_restored):
    """
    Mixing the kinds is the old refusal: the root is the one base the books take, and a
    repository beside it is an extra one.
    """
    media = [rocky_root(str(tmp_path / "mirror"), "9.8"), _repository(tmp_path, BASEOS)]
    code, result, _, started, commands, _ = _build_of_a_list(tmp_path, monkeypatch, media)
    assert code == 1 and result["reason"].startswith(rules.REFUSALS["extra"])
    assert started == [] and commands.of("mount") == []


def test_another_family_has_no_use_for_a_repository_list(tmp_path, monkeypatch, sigterm_restored):
    """
    A list is for the Red Hat family: an Ubuntu record naming dnf repositories finds no
    identifying source, as it always did.
    """
    media = [_repository(tmp_path, BASEOS)]
    code, result, _, started, commands, _ = _build_of_a_list(tmp_path, monkeypatch, media,
                                                             distribution="ubuntu", osrelease="24.04")
    assert code == 1 and result["reason"] == rules.REFUSALS["no_identity"]
    assert started == [] and commands.of("mount") == []


def test_books_that_do_not_take_a_repository_list_are_refused_before_anything_is_mounted(
        tmp_path, monkeypatch, sigterm_restored):
    """
    A checkout whose seed has no image_el_repos would build the image as if no list had been
    given, so the build stops naming the books.
    """
    media = [_repository(tmp_path, BASEOS)]
    code, result, _, started, commands, _ = _build_of_a_list(tmp_path, monkeypatch, media, books=False)
    assert code == 1
    assert result["reason"] == (f"the books at {tmp_path / 'repo' / 'site'} do not take a repository list "
                                f"yet: update the books or name a mirror root")
    assert started == [] and commands.of("mount") == []


def test_a_failed_repository_list_build_still_unmounts_and_says_what_the_table_still_shows(
        tmp_path, monkeypatch, sigterm_restored):
    """
    A book that fails ends the build as any does, with the directories unmounted; a mount the
    table still shows after the unmount is in the result and the log, and its directory stays.
    """
    media = [_repository(tmp_path, BASEOS)]
    commands = Commands()
    tree = os.path.realpath(tmp_path / "images" / "rocky9")
    target = f"{tree}/mnt/osimage-repos/{BASEOS}"
    # Nothing is mounted when the build starts; the target still is when it has been read back.
    tables = [Ran(""), Ran(target)]
    commands.answers["findmnt"] = lambda: tables.pop(0) if len(tables) > 1 else tables[0]
    code, result, _, _, commands, jobdir = _build_of_a_list(tmp_path, monkeypatch, media, code=2,
                                                            commands=commands)
    assert code == 1
    assert [argv[-1] for argv in commands.of("umount")] == [target]
    assert result["mounts_left"] == [target]
    assert os.path.isdir(target), "a directory that still holds a mount is not removed"
    assert f"still mounted: {target}" in open(os.path.join(jobdir, "build.log")).read()


def test_a_repository_inside_the_image_tree_is_refused_before_anything_is_mounted(
        tmp_path, monkeypatch, sigterm_restored):
    """
    A directory that holds the tree, or is held by it, would be bound into itself.
    """
    inside = _repository(tmp_path / "images" / "rocky9", "srv/" + BASEOS)
    code, result, _, started, commands, _ = _build_of_a_list(tmp_path, monkeypatch, [inside])
    tree = os.path.realpath(tmp_path / "images" / "rocky9")
    assert code == 1
    assert result["reason"] == f"{os.path.realpath(inside)} and the image tree {tree} contain each other"
    assert started == [] and commands.of("mount") == []


def test_a_bind_that_fails_ends_the_build_and_leaves_no_directory_in_the_image(
        tmp_path, monkeypatch, sigterm_restored):
    """
    The directories made for a mount that did not happen are not left in the image, and the
    reason is the mount's own.
    """
    commands = Commands()
    commands.answers["mount"] = BackendFailure("mount: permission denied")
    code, result, _, started, _, _ = _build_of_a_list(tmp_path, monkeypatch,
                                                      [_repository(tmp_path, BASEOS)], commands=commands)
    assert code == 1 and "permission denied" in result["reason"]
    assert started == []
    assert not os.path.exists(tmp_path / "images" / "rocky9" / "mnt" / "osimage-repos")


def test_what_a_runner_recorded_below_the_tree_is_lazily_unmounted_when_the_job_is_finished(
        tmp_path, monkeypatch):
    """
    A runner that died leaves the daemon the list of what it bound under the image: the ones the
    table shows are released lazily, and the ones already gone are left alone.
    """
    root = os.path.realpath(tmp_path)
    mounted, gone = f"{root}/images/x/mnt/osimage-repos/a", f"{root}/images/x/mnt/osimage-repos/b"
    calls, tables = [], ["", mounted, ""]

    def run(argv, **kwargs):
        calls.append(argv)
        return Ran(tables.pop(0)) if argv[0] == "findmnt" else Ran()
    monkeypatch.setattr(mounts.shell, "run", run)
    jobs.create(str(tmp_path / "state"), "img", REQUEST, VERDICT)
    jobdir = jobs.path(str(tmp_path / "state"), "img")
    jobs.move(jobdir, "running", unit="trixd-osimage-img")
    jobs.files.write(os.path.join(jobdir, jobs.RESULT), json.dumps({"ok": False, "exit": 143, "reason": "x"}))
    (tmp_path / "state" / "img" / "tree-mounts").write_text(f"{mounted}\n{gone}\n")
    jobs.finish(jobdir, "failed")
    assert [argv for argv in calls if argv[0] == "umount"] == [["umount", "-l", "--", mounted]]
