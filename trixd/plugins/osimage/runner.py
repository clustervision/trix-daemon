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
The build of one image tree, run as a child process of the daemon, in a session of its own:

    python3 -m trixd.plugins.osimage.runner <jobdir>

It fetches the books when the checkout is not there, checks what they need on the
controller, reads the job's sources and decides what they build, or what the books build
with no source, mounts a disc the verdict named, registers the binfmt handler of a
foreign arch, writes the inventory, runs the compute book through the lchroot connection
with Luna's own pack left out, relays the book's lines into the job directory,
checks what was built, unmounts, and records how it ended. Every refusal is a result
naming it: the daemon answered Luna before any of this ran. It deletes nothing: a tree
that failed is kept for Luna to remove. With the builder setting at a stub it runs no
book and writes nothing into the tree: it says a few lines and reports a kernel, so the
chain around the build can be exercised.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import fcntl
import glob
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback

from trixd.common.errors import BadRequest, BackendFailure, ServiceUnavailable
from trixd.plugins.osimage import checkout, detect, jobs, mounts, rules
from trixd.utils import files, shell

EXIT_OK: int = 0
EXIT_FAILED: int = 1
EXIT_CANCELLED: int = jobs.EXIT_CANCELLED
# Two registrations of the binfmt handler at once make the second fail, so every runner
# on the controller takes this lock around its own.
QEMU_LOCK: str = "/run/lock/trixd-osimage-qemu.lock"
REMOTE_TMP: str = "/tmp/.ansible-lpart"
# The plays of the compute books left to Luna: its own pack. The record is made by the daemon
# that asked for the build; the books' last play takes it as it is and makes the group.
SKIP_TAGS: tuple = ("pack-images",)
CHROOT_COMMAND: str = "lchroot --path {{ image_path }} --force --"
CONTROLLER_INTERPRETER: str = "/usr/bin/python3"
# How long a reason may be: the last fatal line can carry a package manager's whole output.
REASON_MOST: int = 400
# What builds the tree: the books, or a stub that only says lines, so the whole chain from
# Luna's create to its pack can be exercised without building; stub-fail ends as a failed
# book does. The kernel version a stub reports when the tree holds none.
BOOKS: str = "books"
STUB: str = "stub"
STUB_FAIL: str = "stub-fail"
BUILDERS: tuple = (BOOKS, STUB, STUB_FAIL)
STUB_KERNEL: str = "0.0.0-stub"
# What the books need on the controller, checked before any source is read: the tools
# that keep the build out of a hand chroot, the collections they import and the Galaxy
# role they name, and how long a probe of a command may take.
TOOLS: tuple = ("lchroot", "bwrap")
COLLECTIONS: tuple = ("community.general", "ansible.posix", "community.crypto",
                      "community.mysql", "ansible.mysql")
GALAXY_ROLE: str = "OndrejHome.pcs-modules-2"
PROBE_TIMEOUT: int = 30
# Where the runner looks for a command after its interpreter's own directory.
SYSTEM_PATH: tuple = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
# The oldest Python ansible-core drives on the controller side; EL8's is 3.6.
INTERPRETER_LEAST: tuple = (3, 7)

# The lines of the book's output that are relayed, the ones that start a fatal (ERROR! is
# what ansible says before any play runs), and the ones that tell whether it ended the run.
RELAYED: tuple = ("PLAY [", "TASK [", "RUNNING HANDLER [", "PLAY RECAP")
FATAL: tuple = ("fatal: ", "ERROR!")
CONTINUES: tuple = ("PLAY [", "TASK [", "RUNNING HANDLER [")
ENDS: tuple = ("PLAY RECAP", "NO MORE HOSTS LEFT")
IGNORING: str = "...ignoring"
PADDING = re.compile(r" \*+$")


class Failed(Exception):
    """
    The build stopped, with the reason the result carries.
    """


class Progress:
    """
    The lines worth relaying, each with a status. A fatal or ERROR! line is held until the run
    shows whether it mattered: ansible prints ...ignoring under one it was told to
    ignore, which is not relayed (the job's log keeps it), and goes on to the next task
    after one a rescue caught, but ends the run after any other, so only what follows
    can tell them apart. The exit code decides the build either way; the status only
    decides what the reader prints beside it.
    """

    def __init__(self) -> None:
        """
        Nothing seen yet.
        """
        self.pending = None
        self.fatal: str = ""

    def feed(self, line: str) -> list:
        """
        The entries one line of output yields, as (message, status) pairs.
        """
        text = PADDING.sub("", line.rstrip())
        entries = []
        if self.pending is not None:
            if text == IGNORING:
                self.pending = None
            elif text.startswith(CONTINUES):
                entries.append((self.pending, 200))
                self.pending = None
            elif text.startswith(ENDS):
                # Only a fatal the run ended on is the reason; an ignored or rescued one
                # blamed a task the run had already gone past.
                self.fatal = self.pending
                entries.append((self.pending, 500))
                self.pending = None
        if text.startswith(FATAL):
            if not text.endswith(IGNORING):
                self.pending = text
        elif text.startswith(RELAYED):
            entries.append((text, 200))
        return entries

    def flush(self) -> list:
        """
        What is still held once the output ended: a fatal nothing followed ended the run.
        """
        held, self.pending = self.pending, None
        if held is None:
            return []
        self.fatal = held
        return [(held, 500)]


def toolchain(settings: dict, name: str) -> str:
    """
    A command of the books' toolchain: the interpreter's own when its directory carries one,
    else the system's, which the installer's prepare step puts there. When neither exists,
    the interpreter's path, so a refusal names the first place looked.
    """
    own = os.path.join(settings["python"], "bin")
    for directory in (own,) + SYSTEM_PATH:
        found = os.path.join(directory, name)
        if os.path.isfile(found) and os.access(found, os.X_OK):
            return found
    return os.path.join(own, name)


def playbook_argv(request: dict, verdict: dict, settings: dict) -> list:
    """
    The ansible-playbook command line, every value its own argument. Each variable of
    the verdict travels as a one-key JSON document: key=value is split on whitespace by
    ansible, and JSON also keeps a list or a boolean its type on the other side.
    """
    skipped = ",".join(SKIP_TAGS + tuple(verdict.get("skip_tags") or ()))
    argv = [toolchain(settings, "ansible-playbook"), verdict["playbook"],
            "-e", f"image_name={request['name']}", "-e", f"trinityx_site={settings['books']}",
            "-e", f"ansible_remote_tmp={REMOTE_TMP}", "--skip-tags", skipped,
            "-e", "ansible_service_mgr=systemd", "-e", "ansible_become=false",
            "-e", "image_proc_mount=false", "-e", f"trix_images={settings['images_root']}",
            "-e", json.dumps({"image_chroot_cmd": CHROOT_COMMAND})]
    for key, value in verdict["variables"].items():
        argv += ["-e", json.dumps({key: value})]
    return argv


# Where the installer's prepare step puts the Galaxy roles the books name (pcs-modules).
GALAXY_ROLES: tuple = ("~/.ansible/roles", "/usr/share/ansible/roles", "/etc/ansible/roles")


def ansible_library(settings: dict) -> str:
    """
    The directory of the modules luna2-utils ships for images whose Python is 3.6, which
    run on the image's own Python: dnf and selinux. A Failed naming the directory when
    the interpreter's luna2-utils carries none.
    """
    found = sorted(glob.glob(os.path.join(settings["python"], "lib", "python3*", "site-packages",
                                          "utils", "ansible_library")))
    if not found or not all(os.path.isfile(os.path.join(found[0], f"{module}.py"))
                            for module in ("dnf", "selinux")):
        raise Failed(f"luna2-utils under {settings['python']} ships no dnf and selinux modules "
                     f"for an image whose Python is 3.6: update luna2-utils")
    return found[0]


def playbook_env(settings: dict, inventory: str, library: str = "") -> dict:
    """
    What the book's process is told beyond the environment the build started with: the lchroot connection
    and where its plugin is, the books' roles then the installer's Galaxy roles, the
    collections where the settings say, else on ansible's own search path, the inventory,
    and the directory of modules for an image whose Python is 3.6 when it is given.
    """
    books = settings["books"]
    env = {"LUNA_IMAGE_CONNECTION": "lchroot",
           "ANSIBLE_CONNECTION_PLUGINS": os.path.join(books, "plugins", "connection"),
           "ANSIBLE_ROLES_PATH": ":".join([os.path.join(books, "roles"), *GALAXY_ROLES]),
           "ANSIBLE_COLLECTIONS_SCAN_SYS_PATH": "False",
           "ANSIBLE_HOST_KEY_CHECKING": "False",
           "ANSIBLE_INVENTORY": inventory,
           "PYTHONUNBUFFERED": "1"}
    if settings["collections"]:
        env["ANSIBLE_COLLECTIONS_PATH"] = settings["collections"]
    if library:
        env["ANSIBLE_LIBRARY"] = library
    return env


def controller_name(books: str) -> str:
    """
    This controller's short name when the books bring no hosts file of their own, as in a
    downloaded checkout, and nothing when they do.
    """
    if os.path.isfile(os.path.join(books, "hosts")):
        return ""
    return socket.gethostname().split(".")[0]


def write_inventory(jobdir: str, name: str, interpreter: str, answers: str = "",
                    controller_host: str = "") -> str:
    """
    The run's own inventory beside the books' one: the interpreter of each host as
    inventory data, because an extra variable would also reach a task delegated to the
    other side and run it on the wrong Python. The cluster's answers, when the books carry
    none, go in beside the interpreter, as a directory of files because ansible reads
    group_vars/all.yml only where there is no group_vars/all directory. When the books have
    no hosts file, the controllers group is this controller, which the playbooks' controller
    plays need. Answers the path of its hosts file.
    """
    where = os.path.join(jobdir, jobs.INVENTORY)
    controller = f"ansible_python_interpreter: {CONTROLLER_INTERPRETER}\n"
    hosts = "all: {}\n"
    if controller_host:
        hosts = (f"all:\n  children:\n    controllers:\n      hosts:\n        {controller_host}:\n"
                 "          ansible_host: 127.0.0.1\n          ansible_connection: local\n")
    files.write(os.path.join(where, "hosts.yml"), hosts, 0o600)
    files.write(os.path.join(where, "group_vars", "all", "controller.yml"), controller, 0o600)
    if answers:
        files.write(os.path.join(where, "group_vars", "all", "answers.yml"), answers, 0o600)
    files.write(os.path.join(where, "host_vars", "localhost.yml"), controller, 0o600)
    files.write(os.path.join(where, "host_vars", f"{name}.osimages.luna.yml"),
                f"ansible_python_interpreter: {interpreter}\n", 0o600)
    return os.path.join(where, "hosts.yml")


def inventory_sources(books: str, hosts: str) -> str:
    """
    The books' own inventory line with the run's hosts file after it.
    """
    for line in files.read_text(os.path.join(books, "ansible.cfg")).split("\n"):
        found = re.match(r"^inventory\s*=\s*(.+)$", line.strip())
        if found:
            return f"{found.group(1).strip()},{hosts}"
    raise Failed(f"no inventory line in {books}/ansible.cfg")


def absent_collections(settings: dict) -> list:
    """
    The collections the books import that ansible does not find on its own search path,
    as its ansible-galaxy lists them: a document of the paths, each naming its collections.
    """
    galaxy = toolchain(settings, "ansible-galaxy")
    argv = [galaxy, "collection", "list", "--format", "json"]
    try:
        listed = shell.run(argv, timeout=PROBE_TIMEOUT)
    except BackendFailure as error:
        raise Failed(f"{galaxy} could not be run: {error}") from None
    try:
        paths = json.loads(listed.out) if listed.ok else None
    except ValueError:
        paths = None
    if not isinstance(paths, dict):
        raise Failed(f"{galaxy} did not list the collections: "
                     f"{listed.err or listed.out or 'it said nothing'}")
    present = {name for found in paths.values() for name in found}
    return [name for name in COLLECTIONS if name not in present]


def preconditions(settings: dict) -> None:
    """
    What the book will need, checked before any source is read, so a build does not
    fail an hour in: the books, their collections and Galaxy role, the tools that keep
    the build out of a hand chroot, the controller's Python and the playbook command.
    """
    absent = checkout.missing(settings["repo"])
    if absent:
        raise Failed(f"the books are not at {settings['books']}: {', '.join(absent)} missing")
    collections = settings["collections"]
    if collections:
        missing = [name for name in COLLECTIONS if not os.path.isdir(
            os.path.join(collections, "ansible_collections", *name.split(".")))]
    else:
        missing = absent_collections(settings)
    if missing:
        where = f"at {collections}" if collections else "on ansible's search path"
        raise Failed(f"the collections are not {where}: {', '.join(missing)} missing")
    if not any(os.path.isdir(os.path.join(os.path.expanduser(where), GALAXY_ROLE))
               for where in GALAXY_ROLES):
        raise Failed(f"the Galaxy role {GALAXY_ROLE} is not in {', '.join(GALAXY_ROLES)}")
    # Without both, the books fall back to a native chroot with hand mounts in the
    # tree, which a build stopped half way must never have made.
    missing = [tool for tool in TOOLS if not shutil.which(tool)]
    if missing:
        raise Failed(f"{' and '.join(missing)} not on PATH, and without them the books "
                     f"would chroot by hand")
    controller_interpreter()
    playbook = toolchain(settings, "ansible-playbook")
    try:
        version = shell.run([playbook, "--version"], timeout=PROBE_TIMEOUT)
    except BackendFailure as error:
        raise Failed(f"{playbook} could not be run: {error}") from None
    if not version.ok or "core 2." not in version.out.split("\n")[0]:
        raise Failed(f"{playbook} is not ansible-core 2: "
                     f"{version.err or version.out or 'it said nothing'}")


def controller_interpreter() -> None:
    """
    The books run their controller-side tasks on the system Python, which has to be
    one ansible-core can drive; EL8's dnf bindings exist only for its 3.6.
    """
    try:
        asked = shell.run([CONTROLLER_INTERPRETER, "-c",
                           "import sys; print('%d.%d' % sys.version_info[:2])"],
                          timeout=PROBE_TIMEOUT)
    except BackendFailure as error:
        raise Failed(f"{CONTROLLER_INTERPRETER} could not be run: {error}") from None
    found = re.match(r"^(\d+)\.(\d+)$", asked.out.strip()) if asked.ok else None
    if not found:
        raise Failed(f"{CONTROLLER_INTERPRETER} did not say its version: "
                     f"{asked.err or asked.out or 'it said nothing'}")
    if tuple(int(part) for part in found.groups()) < INTERPRETER_LEAST:
        raise Failed(f"this controller's {CONTROLLER_INTERPRETER} is {found.group(0)}; the "
                     f"books need {'.'.join(map(str, INTERPRETER_LEAST))} or newer here "
                     f"(EL9 or later)")


def controller_release() -> dict:
    """
    What this controller runs, from its own os-release, read as detection reads a root:
    the books build a RedHat image with no source from its repositories.
    """
    found = detect.Finding(source="/")
    detect.from_os_release(detect.LocalDir("/"), found)
    return found


def kernel_version(tree: str) -> str:
    """
    The newest kernel installed in the tree that is not a debug one, or nothing.
    """
    modules = os.path.join(tree, "lib", "modules")
    if not os.path.isdir(modules):
        return ""
    plain = [version for version in os.listdir(modules) if not version.endswith("+debug")]
    return max(plain, key=_version_key) if plain else ""


def client_version(tree: str) -> str:
    """
    The version of the luna2-client package in the tree, asked of the tree's own package
    manager through lchroot, or nothing when it is not installed.
    """
    if os.path.isdir(os.path.join(tree, "var", "lib", "dpkg")):
        query = ["dpkg-query", "-W", "-f=${Version}", "luna2-client"]
    else:
        query = ["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", "luna2-client"]
    done = shell.run(["lchroot", "--path", tree, "--force", "--", *query], timeout=300)
    return done.out if done.ok else ""


class Build:
    """
    One run of the job directory given, from the first mount to result.json.
    """

    def __init__(self, jobdir: str) -> None:
        """
        Nothing opened yet.
        """
        self.jobdir = jobdir
        self.log = None
        self.progress = None
        self.seq: int = 0
        self.child = None
        self.parser = Progress()
        self.cancelled: bool = False
        self.bound: list = []
        self.made: list = []

    def run(self) -> int:
        """
        Build, and whatever happens leave result.json behind: the daemon reads nothing
        else to learn how the build ended.
        """
        self.log = _append(os.path.join(self.jobdir, jobs.LOG))
        self.progress = _append(os.path.join(self.jobdir, jobs.PROGRESS))
        # The handler writes to both files, so it is live only while they are open.
        signal.signal(signal.SIGTERM, self._terminated)
        result = {"ok": False, "exit": EXIT_FAILED, "reason": "", "kernelversion": "",
                  "client_version": "", "mounts_left": []}
        try:
            self._build(result)
        except (Failed, BackendFailure) as why:
            result["reason"] = str(why)[:REASON_MOST]
        except Exception as error:
            result["reason"] = f"runner error: {error!r}"[:REASON_MOST]
            self.log.write(traceback.format_exc())
        finally:
            if self.child is not None and self.child.poll() is None:
                # The reader ended before the book did: the book is told to stop
                # before the unmount, or it goes on writing into the tree.
                _terminate(self.child)
            if self.cancelled:
                result.update({"ok": False, "exit": EXIT_CANCELLED, "reason": "cancelled"})
            self._release(result)
            self.say(f"{'done' if result['ok'] else result['reason']}")
            files.write(os.path.join(self.jobdir, jobs.RESULT), json.dumps(result, indent=1), 0o600)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            self.log.close()
            self.progress.close()
        return result["exit"]

    def _build(self, result: dict) -> None:
        """
        The steps in order; a step that cannot go on raises with the reason.
        """
        request = jobs.read_json(self.jobdir, jobs.REQUEST)
        settings = dict(request["settings"], books=checkout.site(request["settings"]["repo"]))
        tree = request["path"]
        jobs.write_phase(self.jobdir, "prepare")
        if settings["builder"] != BOOKS:
            self._stub(settings, tree, result)
            return
        try:
            checkout.ensure(settings["repo"], settings["repo_url"], self.say)
        except ServiceUnavailable as error:
            raise Failed(error.message) from None
        preconditions(settings)
        if os.path.isdir(tree):
            # The books write into the tree and a leftover mount there is a door onto
            # the host; Luna's remove unmounts lazily below the tree before removing it.
            left = mounts.targets_below(tree)
            if left:
                raise Failed(f"something is mounted below {tree}: {', '.join(left)}; "
                             f"unmount it first")
        verdict = self._decide(request, settings["books"])
        variables = dict(verdict["variables"])
        if verdict["base"] is not None and verdict["sources"][verdict["base"]]["kind"] == "iso":
            base = verdict["sources"][verdict["base"]]
            variables[verdict["base_variable"]] = self._mount_disc(base["source"], verdict)
        self._bind_repositories(verdict, tree)
        if verdict["arch"] != platform.machine():
            self._register_binfmt(settings["python"], verdict["arch"])
        verdict = dict(verdict, variables=variables)
        hosts = write_inventory(self.jobdir, request["name"], verdict["interpreter"],
                                self._answers(settings), controller_name(settings["books"]))
        argv = playbook_argv(request, verdict, settings)
        env = dict(os.environ)
        env.update(playbook_env(settings, inventory_sources(settings["books"], hosts),
                                ansible_library(settings) if verdict.get("library") else ""))
        if self.cancelled:
            # The stop arrived during the preparation: the book is not started.
            return
        code = self._play(argv, env, settings["books"])
        if self.cancelled:
            return
        if code != 0:
            suffix = f": {self.parser.fatal}" if self.parser.fatal else ""
            raise Failed(f"ansible-playbook exited {code}{suffix}")
        jobs.write_phase(self.jobdir, "contract")
        result["kernelversion"] = kernel_version(tree)
        if not result["kernelversion"]:
            raise Failed(f"no plain kernel under {tree}/lib/modules")
        result["client_version"] = client_version(tree)
        if not result["client_version"]:
            raise Failed("the luna2-client package is not installed in the image")
        self.say(f"kernel {result['kernelversion']}, luna2-client {result['client_version']}")
        result.update({"ok": True, "exit": EXIT_OK})

    def _decide(self, request: dict, books: str) -> dict:
        """
        What the books are given: the sources read and the rules applied, with the record's
        distribution and release held against them, or what the books build with no
        source, and what the books at this path can do with it. The verdict is written
        beside the request, and a refusal is the reason the build failed.
        """
        arch = request["architecture"]
        try:
            if request["media"]:
                findings = detect.identify_all(request["media"], want_arch=arch,
                                               release=request["osrelease"])
                if rules.is_repository_list(findings, request["distribution"]):
                    verdict = rules.repository_verdict(findings, request["distribution"],
                                                       request["osrelease"], arch)
                else:
                    verdict = rules.verdict(findings, arch)
                    rules.check_record(verdict, request["distribution"], request["osrelease"])
            else:
                verdict = rules.placement(request["distribution"], request["osrelease"], arch,
                                          controller_release(), platform.machine())
            rules.check_books(verdict, books)
        except BadRequest as error:
            raise Failed(error.message) from None
        files.write(os.path.join(self.jobdir, jobs.VERDICT), json.dumps(verdict, indent=1), 0o600)
        self.say(rules.rung(request, verdict))
        self.say(f"building {verdict['detected']}")
        return verdict

    def _stub(self, settings: dict, tree: str, result: dict) -> None:
        """
        The build without the books: a few lines a gap apart, no book and no write into
        the tree, then the kernel of whatever the tree holds, or a placeholder when it
        holds none. The failing stub ends where the book's failure is reported.
        """
        gap = settings["stub_gap_seconds"]
        for said in (f"stub builder: no book runs and {tree} is left as it is",
                     f"stub builder: waiting {gap} s as a build would"):
            if self.cancelled:
                return
            self.say(said)
            time.sleep(gap)
        if settings["builder"] == STUB_FAIL:
            raise Failed("the stub builder failed, as builder stub-fail asks")
        jobs.write_phase(self.jobdir, "contract")
        result["kernelversion"] = kernel_version(tree)
        if result["kernelversion"]:
            self.say(f"kernel {result['kernelversion']} from {tree}/lib/modules")
        else:
            result["kernelversion"] = STUB_KERNEL
            self.say(f"kernel {STUB_KERNEL}, a placeholder: {tree}/lib/modules holds none")
        result.update({"ok": True, "exit": EXIT_OK})

    def _answers(self, settings: dict) -> str:
        """
        The cluster's answers for the run's own inventory: nothing when the checkout
        carries them, which ansible finds beside the playbooks, else the installer's copy.
        """
        if checkout.has_site_answers(settings["repo"]):
            return ""
        if not os.path.isfile(settings["answers"]):
            raise Failed(f"no answer file at {settings['answers']} or in {settings['books']}")
        self.say(f"answers from {settings['answers']}")
        return files.read_text(settings["answers"])

    def _mount_disc(self, iso: str, verdict: dict) -> str:
        """
        The disc the verdict chose as the base, mounted under the job directory and laid
        out as the mirror the books expect; never under the images root.
        """
        number = str(verdict["base"])
        mount = mounts.loop_mount_ro(iso, os.path.join(self.jobdir, jobs.MOUNTS, number))
        url = mounts.mirror_layout(mount, os.path.join(self.jobdir, jobs.REPOS, number),
                                   verdict["arch"])
        self.say(f"mounted {iso} read-only as {url}")
        return url

    def _bind_repositories(self, verdict: dict, tree: str) -> None:
        """
        The repositories on this controller's own disk, bound read-only under the image where
        its package manager reads them: the sandbox the books enter the image through shows
        the tree and nothing of the controller beside it. Each target is written to the job
        directory before it is mounted, so a runner that dies leaves the daemon the list to
        release; a repository and the tree may not contain each other.
        """
        base = os.path.realpath(tree)
        place = os.path.join(base, rules.IMAGE_REPOSITORIES.lstrip("/"))
        for repository in verdict.get("repositories", ()):
            if repository["path"] is None:
                continue
            source = os.path.realpath(repository["path"])
            if not os.path.isdir(source):
                raise Failed(f"{repository['path']} is not a directory on this controller")
            if (source + "/").startswith(base + "/") or (base + "/").startswith(source + "/"):
                raise Failed(f"{source} and the image tree {base} contain each other")
            target = os.path.join(place, repository["id"])
            with open(os.path.join(self.jobdir, jobs.TREE_MOUNTS), "a", encoding="utf-8") as listing:
                listing.write(target + "\n")
            self.made += [made for made in mounts.bind_ro(source, target) if made.startswith(place)]
            self.bound.append(target)
            self.say(f"bound {source} read-only in the image at "
                     f"{os.path.join(rules.IMAGE_REPOSITORIES, repository['id'])}")

    def _register_binfmt(self, python: str, arch: str) -> None:
        """
        The qemu binfmt handler of a foreign arch, one registration at a time. lchroot
        registers again at every entry by itself and finds it done.
        """
        os.makedirs(os.path.dirname(QEMU_LOCK), exist_ok=True)
        with open(QEMU_LOCK, "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            done = shell.run([os.path.join(python, "bin", "python3"), "-m", "utils.qemu_static",
                              arch], timeout=60)
        if not done.ok:
            raise Failed(f"qemu_static could not register the {arch} binfmt handler: "
                         f"{done.err or done.out}")
        self.say(f"registered the {arch} binfmt handler")

    def _play(self, argv: list, env: dict, cwd: str) -> int:
        """
        Run the book and relay its output line by line as it comes, for hours if need
        be, which is why this is the one Popen outside trixd.utils.shell: that helper
        answers when the child has ended, and the reader wants the lines as they are
        written.
        """
        self.log.write(f"[runner] {' '.join(argv)}\n")
        try:
            # A package manager's output is not always UTF-8, and one bad byte must not
            # end the build with the book still running.
            self.child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, errors="replace", bufsize=1,
                                          start_new_session=True, env=env, cwd=cwd)
        except OSError as error:
            raise Failed(f"cannot start {argv[0]}: {error}") from None
        for line in self.child.stdout:
            self.log.write(line)
            self.log.flush()
            for message, status in self.parser.feed(line):
                self._emit(message, status)
        for message, status in self.parser.flush():
            self._emit(message, status)
        return self.child.wait()

    def _release(self, result: dict) -> None:
        """
        Unmount what was mounted and record what the table still shows.
        """
        try:
            bound_left = mounts.unmount_listed(self.bound)
            result["mounts_left"] = mounts.unmount_all(os.path.join(self.jobdir, jobs.MOUNTS)) + bound_left
            # A directory with a mount still in it is not removed, whatever rmdir would say.
            mounts.remove_made([made for made in self.made
                                if not any(left == made or left.startswith(made + "/")
                                           for left in bound_left)])
        except BackendFailure as error:
            result["reason"] = f"{result['reason']}; the mount table could not be read back: {error}"
        if result["mounts_left"]:
            self.log.write(f"[runner] still mounted: {', '.join(result['mounts_left'])}\n")

    def _terminated(self, signum, frame) -> None:
        """
        SIGTERM from the build being stopped: the book's process group is told the same,
        and the loop reading it ends when it has.
        """
        self.cancelled = True
        self.say("cancelled, stopping the playbook")
        if self.child is not None and self.child.poll() is None:
            _terminate(self.child)

    def say(self, message: str) -> None:
        """
        A line of the runner's own, in the log and relayed.
        """
        self.log.write(f"[runner] {message}\n")
        self.log.flush()
        self._emit(message, 200)

    def _emit(self, message: str, status: int) -> None:
        """
        One relayed line, and the phase when it opens a play.
        """
        self.progress.write(json.dumps({"seq": self.seq, "message": message, "status": status}) + "\n")
        self.progress.flush()
        self.seq += 1
        if message.startswith("PLAY [") and "]" in message:
            jobs.write_phase(self.jobdir, message[6:message.index("]")])


def _terminate(child) -> None:
    """
    SIGTERM to the book and everything it started, which share its session.
    """
    os.killpg(child.pid, signal.SIGTERM)


def _append(where: str):
    """
    A file of the job's, appended to and readable by root alone.
    """
    handle = os.fdopen(os.open(where, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a",
                       encoding="utf-8")
    os.chmod(where, 0o600)
    return handle


def _version_key(version: str) -> list:
    """
    A kernel version as sort -V would order it.
    """
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", version)]


def main(argv: list = None) -> int:
    """
    The entry point of the build.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not os.path.isdir(args[0]):
        print("usage: python3 -m trixd.plugins.osimage.runner <jobdir>", file=sys.stderr)
        return 2
    return Build(args[0]).run()


if __name__ == "__main__":
    sys.exit(main())
