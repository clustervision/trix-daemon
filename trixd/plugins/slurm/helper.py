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
The slurm plugin's helper: drain, resume, save, restore and apply on slurm's files, run by the
plugin's handlers as the account `trinityx-slurm` (slurm's AdminLevel Administrator), never as
root; a save and a restore run with the files' group (root's files, group write, as config-slurm
leaves them) and start no child process.

    setpriv --reuid trinityx-slurm ... -- python3 -I -B helper.py <verb>   < request.json

The request is one JSON object on stdin: `files` (nodes, partitions, gres: the plugin's
declaration), `share_dir` (beside the files: the lock and the mark, one per cluster),
`state_dir` (this controller's: the grant answer), and the route's body. The answer is one JSON
object on stdout, `{ok, code, ...}`, exit 0; `code` is the HTTP status the plugin raises.

Who may ask is not judged here: the plugin asked Luna as the person before it ran the helper.
What a save may write is `rules` (the console's
`slurmrules.py`, byte for byte), slurmlint runs through `lintcheck` (the console's
`slurmcheck.py`); both moved from the console with this code.
"""

__author__      = "Alex Ninaber"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Alex Ninaber"
__status__      = "Development"

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import pwd
import stat
import subprocess  # nosec B404  # scontrol and sacctmgr by absolute path, argv only
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # -I leaves the script's directory out; the rules live here

import rules  # noqa: E402  # the console's slurmrules.py, beside this file

KINDS = ("nodes", "partitions", "gres")
NAMES_MAX = 4096
TEXTS_MAX = 4 * 1024 * 1024   # bytes of a save's three texts, encoded
WAIT = 5.0           # seconds a write waits for the one before it
QUICK = 6            # seconds for scontrol update
GRANT_WAIT = 3       # seconds for sacctmgr's answer: inside GET files' 5
SLOW = 48            # seconds for scontrol reconfigure: with the lock's 5, inside apply's 55
GRANT_TTL = 60.0     # seconds slurm's AdminLevel answer is believed
OWNER = 0            # the six files are root's; the helper writes them by their group
WRITERS = ("save", "restore")   # the verbs run with that group: they start no child process


class Refused(Exception):
    """A refusal with the HTTP status the plugin answers."""

    def __init__(self, code: int, message: str):
        """
        Takes the HTTP status the plugin answers with (400, 409, 502 or 503) and the sentence the
        person reads.
        """
        super().__init__(message)
        self.code = code


def sha(data: bytes) -> str:
    """
    Takes bytes and returns their sha256 as a hex string: the stamp a page sends back to prove it
    saw the files as they are.
    """
    return hashlib.sha256(data).hexdigest()


# -- the six paths ------------------------------------------------------------------------------


def _safe(path: str) -> Path:
    """A declared path resolved once: absolute, in a directory the helper's account may not
    change (not its owner, no group or other write: a TrinityX controller's is slurm's, 0755),
    so the account cannot swap an entry for a link."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise Refused(503, f"not an absolute path: {path!r}")
    real = Path(os.path.realpath(path))
    for folder in real.parents:  # an ancestor the account may change could be swapped whole
        found = os.stat(folder)
        sticky = found.st_mode & stat.S_ISVTX and found.st_uid != os.geteuid()
        if found.st_uid == os.geteuid() or (found.st_mode & 0o022 and not sticky):
            raise Refused(503, f"the helper's account may change {folder}: it writes nothing "
                               "under it (each directory above the files belongs to root or "
                               "slurm, without group or other write)")
    return real


def files_of(request: dict) -> dict:
    """The three slurm files of the request, resolved; refused whole when any is not declared."""
    given = request.get("files")
    if not isinstance(given, dict) or set(given) != set(KINDS):
        raise Refused(503, "no Slurm files configured: the plugin sends nodes, partitions and gres")
    return {k: _safe(given[k]) for k in KINDS}


def backup(path: Path) -> Path:
    """
    Takes a managed file's path and returns the path of its backup beside it (the same name with
    .bkp), which a save writes and a restore reads.
    """
    return path.with_name(path.name + ".bkp")


def put(path: Path, data: bytes) -> None:
    """Write a file in place (its owner, mode and inode stay); never creates one, never follows a link."""
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def ours(path: Path, before: bytes, after: bytes) -> bool:
    """Whether the file holds the helper's bytes: what it wrote, or a part a kill cut short."""
    try:
        now = path.read_bytes()
    except OSError:
        return False
    return now != before and after.startswith(now)


# -- the lock and the mark, on the share --------------------------------------------------------


@contextlib.contextmanager
def locked(share: Path, wait: float = None, clock=time.monotonic, sleep=time.sleep):
    """One save, restore, apply, drain or render at a time across both controllers: a POSIX lock
    (`lockf`, which NFS carries between hosts) on `<share>/lock`, waited for `wait` seconds
    (`WAIT` when not given)."""
    wait = WAIT if wait is None else wait
    fd = os.open(share / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        end = clock() + wait
        while True:
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN) or clock() >= end:
                    raise Refused(409, "another Slurm change (a save, restore, apply, drain or "
                                       "the render) is under way: try again") from None
                sleep(0.1)
        yield
    finally:
        os.close(fd)  # closing drops the lock


def _private(folder: Path) -> None:
    """The share and its mark: the account's or root's, a directory nobody else may write, so no
    one else can plant a mark the helper would act on."""
    found = os.lstat(folder)
    if (not stat.S_ISDIR(found.st_mode) or found.st_uid not in (os.geteuid(), OWNER)
            or found.st_mode & 0o022):
        raise Refused(503, f"{folder} is not the helper's own (owner {found.st_uid}, mode "
                           f"{stat.filemode(found.st_mode)}): the setup makes it the account's, 0700")


def mark(share: Path, steps: list) -> None:
    """Each file's bytes before and after, then the list naming them: a kill before the list is
    written left no file touched; after it, `recover` finds what to put back."""
    pending = share / "pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    for i, (_path, before, after) in enumerate(steps):
        (pending / f"{i}.before").write_bytes(before)
        (pending / f"{i}.after").write_bytes(after)
    (pending / "paths.tmp").write_text(json.dumps([str(p) for p, _b, _a in steps]))
    (pending / "paths.tmp").replace(pending / "paths.json")


def marked(share: Path) -> bool:
    """
    Takes the share directory and returns whether a save or restore left its mark
    (pending/paths.json): a write that was cut and must be put back before anything else.
    """
    return (share / "pending" / "paths.json").exists()


def clear_mark(share: Path) -> None:
    """
    Takes the share directory and removes the mark and every file left beside it in pending/,
    once the write it named is complete or put back. Returns nothing.
    """
    pending = share / "pending"
    (pending / "paths.json").unlink(missing_ok=True)
    for leftover in pending.glob("*.*"):
        leftover.unlink()


def put_back(share: Path, steps: list) -> list:
    """Each file still holding the helper's bytes gets its bytes from before; no other file. A
    file gone since the mark is named, never created."""
    back = []
    for path, before, after in steps:
        if not path.exists():
            back.append(f"{path} is gone since the save was cut: not put back")
        elif ours(path, before, after):
            put(path, before)
            back.append(str(path))
    clear_mark(share)
    return back


def recover(share: Path, files: dict) -> list:
    """Under the lock, before a save or a restore: a write a kill cut short is put back from the
    mark, which may name the six files and nothing else."""
    pending = share / "pending"
    if not (pending / "paths.json").exists():
        return []
    for folder in (share, pending):
        _private(folder)
    try:
        paths = json.loads((pending / "paths.json").read_text())
    except ValueError as error:
        raise Refused(503, f"the mark of a cut save cannot be read ({error}): "
                           f"put the files back by hand, then remove {pending}") from None
    six = {str(q) for p in files.values() for q in (p, backup(p))}
    strange = [p for p in paths if p not in six] if isinstance(paths, list) else [repr(paths)]
    if strange:
        raise Refused(503, "the mark of a cut save names files that are not the six: %s; "
                           "the helper writes none of them" % ", ".join(map(str, strange[:6])))
    steps = [
        (Path(p), (pending / f"{i}.before").read_bytes(), (pending / f"{i}.after").read_bytes())
        for i, p in enumerate(paths)
    ]
    return put_back(share, steps)


# -- slurm --------------------------------------------------------------------------------------


def run(args: list, timeout: int) -> str:
    """
    Runs one Slurm command without a shell. Takes the argv (validated names only) and a timeout
    in seconds; returns its standard output. Raises Refused 502 when it does not finish in time
    (the outcome is unknown) or exits non-zero (with Slurm's own words), and 503 when the command
    cannot run on this controller.
    """
    try:
        done = subprocess.run(  # nosec B603  # argv of validated names, no shell
            args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise Refused(502, f"Slurm did not answer within {timeout} s: the outcome is unknown, "
                           "reload") from None
    except OSError as error:
        raise Refused(503, f"{args[0]} cannot run here: {error}") from None
    if done.returncode:
        said = (done.stderr or done.stdout).strip()
        raise Refused(502, f"{Path(args[0]).name} refused: {said}")
    return done.stdout


def granted(state: Path) -> str:
    """Why slurm does not let this account change nodes, "" when it does; asked once a minute
    per controller (`<state>/granted`, the answer and when)."""
    kept = state / "granted"
    try:
        at, why = json.loads(kept.read_text())
        if 0 <= time.time() - at < GRANT_TTL:
            return why
    except (OSError, ValueError):
        pass
    account = pwd.getpwuid(os.getuid()).pw_name
    try:
        level = run([rules.SACCTMGR, "-n", "-P", "show", "user", account, "format=adminlevel"],
                    GRANT_WAIT).strip()
        why = "" if level in rules.NODE_ADMINS else (
            f"Slurm has not made {account} an Administrator (sacctmgr AdminLevel)")
    except Refused as error:
        why = f"Slurm accounting did not answer: {error}"
    with contextlib.suppress(OSError):
        kept.write_text(json.dumps([time.time(), why]))
    return why


def _not_ours(path: Path, group: int) -> str:
    """Why the helper may not write one of the six, "" when it may: a regular file, not a link,
    root's, of the files' group with group read and write (config-slurm sets them so)."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return f"there is no {path}"
    if not stat.S_ISREG(found.st_mode):
        return f"{path} is not a regular file: the helper writes no link"
    if found.st_uid != OWNER or found.st_gid != group or found.st_mode & 0o060 != 0o060:
        return (f"{path} is not the files' group's to write (owner {found.st_uid}, group "
                f"{found.st_gid}, mode {stat.filemode(found.st_mode)}): the helper writes root's "
                f"files of group {group} with group read and write")
    return ""


def unready(files: dict, group) -> list:
    """Each gap that stops a write, as a sentence."""
    if not isinstance(group, int) or isinstance(group, bool):
        return ["the plugin names no group for the files"]
    paths = [q for p in files.values() for q in (p, backup(p))]
    return [why for why in (_not_ours(p, group) for p in paths) if why]


def ready(files: dict, group) -> None:
    """
    Takes the resolved files and the group that must own them; returns nothing when a write may
    go ahead and raises Refused 503 with every gap unready() finds, joined in one sentence.
    """
    why = unready(files, group)
    if why:
        raise Refused(503, "; ".join(why))


# -- the verbs ----------------------------------------------------------------------------------


def files_verb(request: dict, files: dict, share: Path, state: Path) -> dict:
    """What `GET files` answers: every gap a sentence, `granted` the one drain and resume need,
    the files' stamp and the rules' digest."""
    why_granted = granted(state)
    stamp = {k: sha(p.read_bytes()) for k, p in files.items() if p.is_file()}
    cut = ["a save was cut short: the next save or restore puts the files back"] if marked(share) else []
    return {"ok": True, "code": 200,
            "why": ([why_granted] if why_granted else []) + unready(files, request.get("group")) + cut,
            "granted": why_granted, "stamp": stamp, "rules": sha((HERE / "rules.py").read_bytes()),
            "account": pwd.getpwuid(os.getuid()).pw_name}


def _names(request: dict) -> list:
    """
    Takes the request and returns its node names sorted and without repeats. Raises Refused 400
    when names is not a list of 1 to NAMES_MAX entries or any entry is not a node name by
    rules.NAME.
    """
    names = request.get("names")
    if not isinstance(names, list) or not names or len(names) > NAMES_MAX:
        raise Refused(400, f"names: the list of 1 to {NAMES_MAX} nodes to change")
    bad = [n for n in names if not isinstance(n, str) or not rules.NAME.fullmatch(n)]
    if bad:
        raise Refused(400, "not node names: %s" % ", ".join(map(repr, bad[:20])))
    return sorted(set(names))


def _update(request: dict, files: dict, share: Path, state: Path, how: str, reason: str) -> dict:
    """The nodes Luna renders, set to `how` by `scontrol update`, refused whole otherwise."""
    names = _names(request)
    why = granted(state)
    if why:
        raise Refused(503, why)
    try:
        import hostnames  # the console's copy, beside this file; it imports python-hostlist
    except ImportError as error:
        raise Refused(503, f"the helper cannot fold node names here: {error}") from None
    # The lock is held across scontrol update: a save truncates the file before it writes it,
    # and a render cannot rename a node between this read and the update.
    with locked(share):
        rendered = rules.rendered_nodes(files["nodes"].read_text(encoding="utf-8"))
        unknown = [n for n in names if n not in rendered]
        if unknown:
            raise Refused(400, "not nodes Luna renders into %s: %s"
                          % (files["nodes"], ", ".join(unknown)))
        hosts = hostnames.fold(names)
        args = [rules.SCONTROL, "update", "NodeName=" + hosts, "State=" + how]
        run(args + (["Reason=" + reason] if reason else []), QUICK)
    return {"ok": True, "code": 200, "names": names, "did": f"{how.lower()} {hosts}"}


def drain(request: dict, files: dict, share: Path, state: Path) -> dict:
    """
    Drains the nodes named. Takes the request (names, reason, person), the resolved files, the
    share directory and the grant state file; returns what _update() returns. Raises Refused 400
    when the reason breaks the console's rules (rules.reason_refused) or no person is given; the
    reason Slurm stores names the person (rules.authored).
    """
    reason = str(request.get("reason") or "").strip()
    why = rules.reason_refused(reason)
    if why:
        raise Refused(400, why)
    person = request.get("person")
    if not isinstance(person, str) or not person:
        raise Refused(400, "person: whose drain this is (the plugin sends it)")
    return _update(request, files, share, state, "DRAIN", rules.authored(person, reason))


def resume(request: dict, files: dict, share: Path, state: Path) -> dict:
    """
    Resumes the nodes named. Takes the request (names), the resolved files, the share directory
    and the grant state file; returns what _update() returns. A resume carries no reason.
    """
    return _update(request, files, share, state, "RESUME", "")


def _texts(request: dict, files: dict) -> dict:
    """A save's texts, or a restore's: the backups'."""
    if request.get("mode", "save") not in ("save", "restore"):
        raise Refused(400, "mode: save or restore")
    if request.get("mode", "save") == "restore":
        try:
            return {k: backup(p).read_text(encoding="utf-8") for k, p in files.items()}
        except (OSError, UnicodeDecodeError) as error:
            raise Refused(503, f"the backups cannot be read: {error}") from None
    texts = request.get("texts")
    if (not isinstance(texts, dict) or set(texts) != set(KINDS)
            or not all(isinstance(t, str) for t in texts.values())):
        raise Refused(400, "texts: the new text of %s" % ", ".join(KINDS))
    size = 0
    for kind, text in texts.items():
        try:
            size += len(text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise Refused(400, f"texts: {kind} is not UTF-8 text ({error.reason})") from None
    if size > TEXTS_MAX:
        raise Refused(400, f"texts: {size} bytes, more than the {TEXTS_MAX} a save carries")
    return texts


def _judged(files: dict, request: dict, texts: dict) -> tuple:
    """Under the lock: the files as the page loaded them, Luna's, the changed lines the editor's,
    slurmlint's. `(old bytes, the nodes whose State the texts change)`."""
    ready(files, request.get("group"))
    old = {k: p.read_bytes() for k, p in files.items()}
    loaded = request.get("stamp") if isinstance(request.get("stamp"), dict) else {}
    stale = [str(files[k]) for k in KINDS if loaded.get(k) != sha(old[k])]
    if stale:
        raise Refused(409, rules.STALE % ", ".join(stale))
    try:
        before = {k: v.decode("utf-8") for k, v in old.items()}
    except UnicodeDecodeError as error:
        raise Refused(409, f"the Slurm files are not text the helper reads: {error}") from None
    if "TrinityX" not in rules.split(before["nodes"])[1]:
        raise Refused(409, f"{files['nodes']} has no TrinityX block: the helper writes Luna's files only")
    why = rules.changed_lines_refused(before, texts)
    if why:
        raise Refused(409, "; ".join(why))
    problems = lint(texts)
    if problems:
        raise Refused(400, "slurmlint: " + "; ".join(problems))
    return old, sorted(rules.states_changed(before["nodes"], texts["nodes"]))


def preview(request: dict, files: dict, share: Path, state: Path) -> dict:
    """The nodes whose State a save or a restore would change; a refusal the write would meet
    is said in `refused`, not raised (the console asks before the person saves)."""
    try:
        texts = _texts(request, files)
        with locked(share):
            _old, states = _judged(files, request, texts)
    except Refused as refusal:
        if refusal.code in (409, 400):
            return {"ok": True, "code": 200, "states": [], "refused": str(refusal)}
        raise
    return {"ok": True, "code": 200, "states": states, "refused": ""}


def write(request: dict, files: dict, share: Path, state: Path) -> dict:
    """A save (`mode` save, the backups first) or a restore (the backups' texts, the backups
    kept): judged under the lock, `judged` the State changes Luna was asked about, then the mark,
    the files, the read-back."""
    texts = _texts(request, files)
    with locked(share):
        back = recover(share, files)  # under the lock: never a save in flight
        if back:
            return {"ok": False, "code": 409, "put_back": back,
                    "message": "a save was cut short and its files are put back: reload and redo "
                               "the change"}
        old, states = _judged(files, request, texts)
        if sorted(request.get("judged") or []) != states:
            raise Refused(409, "the State changes judged are not the ones this text makes: reload")
        new = {k: t.encode("utf-8") for k, t in texts.items()}
        steps = [(files[k], old[k], new[k]) for k in KINDS]
        if request.get("mode", "save") == "save":
            steps = [(backup(files[k]), backup(files[k]).read_bytes(), old[k]) for k in KINDS] + steps
        mark(share, steps)
        try:
            for path, _before, after in steps:
                put(path, after)
        except OSError as error:
            put_back(share, steps)
            raise Refused(502, f"writing {path} failed: {error}; what the helper wrote is put back") from None
        clear_mark(share)  # every write is done: a read-back that fails reverts nothing
        lost = [str(p) for p, _b, after in steps if p.read_bytes() != after]
    if lost:
        raise Refused(409, "%s changed while the helper wrote it (Luna re-renders on node changes), "
                           "so it may hold a mix of both writes: Restore puts back the set before "
                           "this save, or reload and redo the change" % ", ".join(lost))
    return {"ok": True, "code": 200, "stamp": {k: sha(v) for k, v in new.items()}}


def apply(request: dict, files: dict, share: Path, state: Path) -> dict:
    """`scontrol reconfigure`: slurmctld refuses what it cannot load and keeps serving."""
    with locked(share):
        if marked(share):
            raise Refused(409, "a save was cut short: save or restore first, then apply")
        try:
            run([rules.SCONTROL, "reconfigure"], SLOW)
        except Refused as refusal:
            raise Refused(refusal.code, f"{refusal}; the saved files stay on disk, where Slurm's "
                                        "commands read them: Restore puts back the set before the "
                                        "last save") from None
    return {"ok": True, "code": 200}


def lint(texts: dict) -> list:
    """slurmlint's lines over the texts; without slurmlint here the helper writes nothing."""
    try:
        import lintcheck  # the console's slurmcheck.py, beside this file; it imports slurmlint
    except ImportError as error:
        raise Refused(503, f"the helper cannot lint a save here: {error}") from None
    return lintcheck.lint(dict(texts))


VERBS = {"files": files_verb, "drain": drain, "resume": resume, "preview": preview,
         "save": write, "restore": write, "apply": apply}


def answer(verb: str, request: dict) -> dict:
    """One verb on one request: the files resolved, then the verb (a save or a restore puts a cut
    write back first, under the lock)."""
    try:
        if verb not in VERBS:
            raise Refused(400, f"no verb {verb!r}")
        files = files_of(request)
        share, state = Path(request["share_dir"]), Path(request["state_dir"])
        if verb in WRITERS:
            request = {**request, "mode": verb}
        return VERBS[verb](request, files, share, state)
    except Refused as refusal:
        return {"ok": False, "code": refusal.code, "message": str(refusal)}


def main() -> int:
    """
    The helper's entry point: reads one JSON request on standard input, runs the verb named by
    the first argument through answer() and writes one JSON answer on standard output ({ok, code,
    message} on a refusal, also when the input is not a JSON object). Always returns 0: the
    outcome is in the answer, never in the exit status.
    """
    try:
        request = json.loads(sys.stdin.read())
    except ValueError as error:
        request = None
        said = {"ok": False, "code": 400, "message": f"the request is not JSON: {error}"}
    if isinstance(request, dict):
        said = answer(sys.argv[1] if len(sys.argv) > 1 else "", request)
    elif request is not None:
        said = {"ok": False, "code": 400, "message": "the request is not a JSON object"}
    sys.stdout.write(json.dumps(said))
    return 0


if __name__ == "__main__":
    sys.exit(main())
