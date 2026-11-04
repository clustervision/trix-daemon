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

"""What the Slurm editor may write, as rules over the files' text. Standard library only: the
plugin's routes read it, and the console runs a copy of it, byte for byte, so a line the editor
cannot make is refused by both. `changed_lines_refused` holds a save: outside the Defaults
and TrinityX blocks every byte stays; inside, each changed line is a note or an entry the editor
writes, with its own keys, keys that entry already carried, and one token per value."""

from __future__ import annotations

import re
from collections.abc import Mapping

SCONTROL = "/usr/bin/scontrol"
SACCTMGR = "/usr/bin/sacctmgr"
#: who may change a node's state in Slurm besides root and SlurmUser: AdminLevel Administrator
#: (an Operator may change the accounting database, not the controller's nodes)
NODE_ADMINS = ("Administrator",)
STALE = (
    "%s changed since this page loaded it (Luna re-renders the files on node changes, or someone"
    " saved): reload and redo the change"
)
#: what the page may set, per table; any other property already in the files is kept as it is
KEYS = {
    "hw": (
        "Boards",
        "SocketsPerBoard",
        "CoresPerSocket",
        "ThreadsPerCore",
        "RealMemory",
        "TmpDisk",
        "CpuBind",
    ),
    "gres": ("Name", "Type", "Count", "File"),
    "node": ("State",),
    "partition": (
        "Default",
        "MaxTime",
        "MaxNodes",
        "OverTimeLimit",
        "State",
        "Shared",
        "AllowAccounts",
        "PowerDownOnIdle",
    ),
}
NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # a preset's name
#: one Slurm token: no space, comment or second '=', and no backslash, which Slurm reads as the
#: line going on into the next
VALUE = re.compile(r"[^\s#=\\]{1,256}")
#: the TrinityX drainer's reason prefix (alertx `config.MARKER`): it resumes by itself a node it
#: drained once the node's NHC alert is gone, so a person's drain never carries it
DRAINER = "TRIX-DRAINER: "
REASON_MAX = 200  # one line a person reads in sinfo's REASON column
#: the account the helper runs as: Slurm records it as a reason's author, so the helper writes the
#: person in front of the reason, and only from it is that prefix believed
ACCOUNT = "trinityx-slurm"
#: a person as Luna names one (the manifest's `person`): either case, digits, `_ . -`,
#: a blank inside, 64 at most
PERSON = r"[A-Za-z0-9_.-](?:[A-Za-z0-9_. -]{0,62}[A-Za-z0-9_.-])?"
_AUTHORED = re.compile(r"\[(%s)\] (.*)" % PERSON, re.DOTALL)


def authored(person: str, reason: str) -> str:
    """A drain's reason as the helper hands it to Slurm: the person first."""
    return "[%s] %s" % (person, reason)


def author_of(reason: str, recorded_by: str) -> tuple[str, str]:
    """(who set the reason, the reason as they wrote it): the person in front of it when Slurm
    records the helper's account as its author, else that account and the reason whole."""
    found = _AUTHORED.fullmatch(reason) if recorded_by == ACCOUNT else None
    return (found[1], found[2]) if found else (recorded_by, reason)


def reason_refused(reason: str) -> str:
    """Why a drain's reason is not one Slurm keeps for a person, "" when it is."""
    if not reason:
        return "a drain needs a reason: Slurm keeps it with the node"
    if len(reason) > REASON_MAX or any(c < " " or c == "\x7f" for c in reason):
        return "the reason is one line of at most %d characters" % REASON_MAX
    if DRAINER.strip() in reason:
        return (
            "the reason may not carry the drainer's mark %r: the drainer would resume the node"
            " by itself" % DRAINER.strip()
        )
    return ""


BLOCKS = ("Defaults", "TrinityX")  # the managed blocks a save writes; any other stays as it is
_START = re.compile(r"#### (.+) Managed block start ####")
_TOKEN = re.compile(r'\s*((?:"[^"]*"|[^\s"#])+)')  # a Slurm token; a quoted part may hold spaces
_END = "#### %s Managed block end   ####"

_ON_NODE = (*KEYS["hw"], *KEYS["node"], "Gres")
#: per file, the entries a save writes: their own keys and what their name must be
ENTRIES: dict[str, dict[str, tuple[tuple[str, ...], str]]] = {
    "nodes": {
        "HWPresetName": ((*KEYS["hw"], *KEYS["node"]), "preset"),
        "NodeName": (_ON_NODE, "node"),
        "PartitionName": (_ON_NODE, "group"),
        "GroupName": (_ON_NODE, "group"),
        "NodeSet": (("Nodes",), "group"),
    },
    "partitions": {"PartitionName": ((*KEYS["partition"], "Nodes"), "group")},
    "gres": {
        "GRESPresetName": ((*KEYS["gres"], "no_consume"), "preset"),
        "Name": ((*KEYS["gres"], "no_consume"), "preset"),
        "NodeGres": (KEYS["gres"], "node"),
        "GroupGres": (KEYS["gres"], "group"),
        "NodeName": ((*KEYS["gres"], "no_consume"), "hostlist"),
    },
}
#: per file, the entries Luna renders into its block, the ones Slurm reads (every other entry
#: lives commented in the Defaults block), and whether one name has one line (a node's gres
#: has a line per resource, rendered from its Defaults entries: when none names it any more, its
#: lines go with them, `_dropped`)
RENDERED: dict[str, dict[str, bool]] = {
    "nodes": {"NodeName": True, "NodeSet": True},
    "partitions": {"PartitionName": True},
    "gres": {"NodeName": False},
}


def split(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """(the lines outside the saved blocks, each block's markers kept; {block: its lines})."""
    outside: list[str] = []
    blocks: dict[str, list[str]] = {}
    inside = ""
    for line in text.split("\n"):
        if inside and line.rstrip() == _END % inside:
            inside = ""
        elif inside:
            blocks[inside].append(line)
            continue
        outside.append(line)
        start = _START.fullmatch(line.rstrip())
        if start and start[1] in BLOCKS:
            inside = start[1]
            blocks.setdefault(inside, [])
    return outside, blocks


def _entry(line: str) -> list[str] | None:
    """A block line's tokens before any comment, its one leading '#' off; None when a token
    cannot be read to its end (an unclosed quote), so nothing behind it goes unread."""
    text = line[1:] if line.startswith("#") else line
    tokens, pos = [], 0
    while match := _TOKEN.match(text, pos):
        tokens.append(match[1])
        pos = match.end()
    rest = text[pos:].lstrip()
    return tokens if not rest or rest.startswith("#") else None


def _carried(blocks: Mapping[str, list[str]]) -> dict[str, set[str]]:
    """{entry ("NodeName=node002"): the keys it carries, and each `key=value` token verbatim}
    over every block of a file. Luna's node line also carries what its group's Defaults entries
    carry (the generator renders a group's `GroupName=` and `PartitionName=` keys onto each of its
    nodes), its group named after the '#'."""
    out: dict[str, set[str]] = {}
    for line in (ln for lines in blocks.values() for ln in lines):
        tokens = _entry(line)
        if tokens and "=" in tokens[0]:
            out.setdefault(tokens[0], set()).update(
                part for t in tokens[1:] for part in {t, t.split("=", 1)[0]}
            )
    for line in blocks.get("TrinityX", []):
        tokens, _hash, note = line.partition("#")
        group = next(
            (t[len("GroupName=") :] for t in note.split() if t.startswith("GroupName=")), ""
        )
        if group and tokens.split():
            for entry in ("GroupName=" + group, "PartitionName=" + group):
                out.setdefault(tokens.split()[0], set()).update(out.get(entry, set()))
    return out


def _names(blocks: Mapping[str, list[str]], kind: str) -> set[str]:
    """The names a file's TrinityX block gives entries of `kind`: Luna's nodes, groups, partitions."""
    return {
        tokens[0].split("=", 1)[1]
        for tokens in map(_entry, blocks.get("TrinityX", []))
        if tokens and tokens[0].startswith(kind + "=")
    }


def rendered_nodes(nodes_text: str) -> set[str]:
    """The nodes Luna renders into its block of the nodes file: Slurm's nodes Luna knows."""
    return _names(split(nodes_text)[1], "NodeName")


def _groups(blocks: Mapping[str, list[str]]) -> dict[str, set[str]]:
    """{group: its nodes}, from the group note on each node line of Luna's block."""
    out: dict[str, set[str]] = {}
    for line in blocks.get("TrinityX", []):
        head, _hash, note = line.partition("#")
        tokens = head.split()
        for word in note.split():
            if tokens and tokens[0].startswith("NodeName=") and word.startswith("GroupName="):
                out.setdefault(word[len("GroupName=") :], set()).add(tokens[0][len("NodeName=") :])
    return out


def _states(blocks: Mapping[str, list[str]]) -> dict[tuple[str, str], str]:
    """{(block, entry): the State it sets, "" for none} over a nodes file's entries: the last
    one, in any case, as Slurm reads a key set twice."""
    return {
        (block, tokens[0]): next(
            (t[len("State=") :] for t in reversed(tokens) if t.lower().startswith("state=")), ""
        )
        for block, lines in blocks.items()
        for tokens in map(_entry, lines)
        if tokens and "=" in tokens[0]
    }


def states_changed(old_nodes: str, new_nodes: str) -> set[str]:
    """Luna's nodes whose Slurm State a save of the nodes file changes: on the node's line in
    Luna's block (what Slurm reads now) or on a Defaults entry that reaches it (what Luna renders
    next): the node's own, its group's, or a preset's, which may reach any node."""
    blocks, new_blocks = split(old_nodes)[1], split(new_nodes)[1]
    groups = _groups(blocks)
    nodes = _names(blocks, "NodeName")
    before, after = _states(blocks), _states(new_blocks)
    out: set[str] = set()
    for entry in {e for (_b, e) in before.keys() | after.keys()}:
        if all(before.get((b, entry), "") == after.get((b, entry), "") for b in BLOCKS):
            continue
        kind, _eq, name = entry.partition("=")
        if kind == "HWPresetName" or (name not in nodes and name not in groups):
            out |= nodes
        else:
            out |= {name} if name in nodes else groups[name]
    return out


def _token_refused(token: str, own: tuple[str, ...], carried: set[str]) -> str:
    """Why one token of an entry is refused, or "" when it may stay. Takes the token (key=value, or
    a bare key), the keys the editor sets for this kind of entry, and the tokens the entry carried
    before the save. A token carried before stays as it was; otherwise the key must be the editor's
    or one the entry carried, a value must be one Slurm value, and only no_consume stands without
    one."""
    if "=" in token and token in carried:
        return ""  # kept as the entry carried it: Slurm took it before (a quoted list, any case)
    key, eq, value = token.partition("=")
    if key not in own and key not in carried:
        return "%s is not a key the editor sets, nor one this entry carried" % key
    if eq and not VALUE.fullmatch(value):
        return "%s=%r is not one Slurm value" % (key, value)
    if not eq and key != "no_consume":
        return "%s carries no value" % key
    return ""


def _line_refused(  # noqa: PLR0913  # one line judged against its file's two tables and scopes
    kinds: Mapping[str, tuple[tuple[str, ...], str]],
    rendered: Mapping[str, bool],
    block: str,
    line: str,
    scope: Mapping[str, set[str]],
    carried: Mapping[str, set[str]],
) -> list[str]:
    """Why one line new in a block is refused, as sentences; empty when the line may be written.
    Takes the file's entry kinds (the keys the editor sets for each and what names it), which kinds
    Luna renders, the block (Defaults or TrinityX), the line, the names Luna renders per scope and
    the tokens each entry carried. A note or a blank line passes; an uncommented line that is no
    entry, an uncommented Defaults entry, a commented entry or one Luna does not render in Luna's
    block, a name Luna does not render and a key set twice are refused; then each token by
    _token_refused."""
    tokens = _entry(line)
    if tokens is None:
        return ["%r: a line the editor cannot read to its end (an unclosed quote)" % line]
    if not any("=" in t for t in tokens):
        if not line.strip() or line.lstrip().startswith("#"):
            return []  # a note: Slurm reads nothing from it
        return [
            "%r: Slurm reads an uncommented line (an `Include` too); the editor writes none" % line
        ]
    if block == "Defaults" and not line.startswith("#"):
        return ["%s: a Defaults entry stays commented, or Slurm reads it" % tokens[0]]
    kind, _eq, name = tokens[0].partition("=")
    if block == "TrinityX" and line.startswith("#"):
        return ["%s: a commented entry in Luna's block: Luna renders none" % tokens[0]]
    if block == "TrinityX" and kind not in rendered:
        return ["%s= is not an entry Luna renders into its block" % kind]
    if kind not in kinds:
        return ["%s= is not an entry the editor writes here" % kind]
    own, names = kinds[kind]
    allowed = {"preset": bool(NAME.fullmatch(name)), "hostlist": bool(VALUE.fullmatch(name))}
    if not allowed.get(names, name in scope.get(names, set())):
        return ["%s is not a %s Luna renders" % (tokens[0], names)]
    keys = [t.partition("=")[0].lower() for t in tokens]
    twice = sorted({t.partition("=")[0] for i, t in enumerate(tokens) if keys[i] in keys[:i]})
    if twice:
        return ["%s: %s is set twice: Slurm keeps only the last" % (tokens[0], k) for k in twice]
    why = (_token_refused(t, own, carried.get(tokens[0], set())) for t in tokens[1:])
    return ["%s: %s" % (tokens[0], w) for w in why if w]


Split = tuple[list[str], dict[str, list[str]]]


def _uncommented(lines: list[str]) -> list[str]:
    """The entries of a block's lines Slurm reads, one per uncommented line, in order."""
    return [
        tokens[0]
        for tokens in (_entry(ln) for ln in lines if not ln.startswith("#"))
        if tokens and "=" in tokens[0]
    ]


def _dropped(
    luna: list[str], kept: list[str], rendered: Mapping[str, bool], defaults: list[str]
) -> set[str]:
    """The entries of Luna's block a save drops that are not the save's to drop: any of one line
    per name; of several lines per name (a node's gres), one a Defaults entry still names, since
    the generator would render it again. Only a NodeGres= entry names a node:
    a preset of the same name does not."""
    named = {
        tokens[0].partition("=")[2]
        for tokens in map(_entry, defaults)
        if tokens and tokens[0].partition("=")[0] == "NodeGres"
    }
    return {
        entry
        for entry in set(luna) - set(kept)
        if rendered.get(entry.partition("=")[0], True) or entry.partition("=")[2] in named
    }


def _file_refused(
    kinds: Mapping[str, tuple[tuple[str, ...], str]],
    old: Split,
    new: Split,
    scope: Mapping[str, set[str]],
    rendered: Mapping[str, bool],
) -> list[str]:
    """Why a save's new text of one file is refused, as sentences; empty when it may be written.
    Takes the file's entry kinds, the old and the new text each split into the lines outside the
    blocks and each block's lines, the names Luna renders per scope, and which kinds Luna renders.
    Refused: a change outside the Defaults and TrinityX blocks, an entry of Luna's block dropped
    that is not the save's to drop (_dropped) or written twice, and each new line _line_refused
    refuses."""
    (outside, blocks), (new_outside, new_blocks) = old, new
    why = [] if new_outside == outside else ["a change outside the Defaults and TrinityX blocks"]
    luna, kept = (_uncommented(b.get("TrinityX", [])) for b in (blocks, new_blocks))
    dropped = _dropped(luna, kept, rendered, new_blocks.get("Defaults", []))
    why += ["%s is Luna's: a save keeps it" % entry for entry in sorted(dropped)]
    why += [
        "%s: a second line for one entry" % entry
        for entry in sorted(set(kept))
        if rendered.get(entry.split("=", 1)[0]) and kept.count(entry) > max(1, luna.count(entry))
    ]
    carried = _carried(blocks)
    for block, lines in new_blocks.items():
        for line in sorted(set(lines) - set(blocks.get(block, []))):
            tokens = _entry(line) or [""]
            if tokens[0] not in dropped:  # a Luna entry commented out is named once, above
                why += _line_refused(kinds, rendered, block, line, scope, carried)
    return why


def changed_lines_refused(old: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    """Why `new` is not a save of `old` the editor could make, one sentence per line refused;
    [] when it is. Both map "nodes", "partitions" and "gres" to the file's text."""
    split_old = {key: split(old[key]) for key in ENTRIES}
    nodes_tx = split_old["nodes"][1]
    scope = {
        "node": _names(nodes_tx, "NodeName"),
        "group": _names(nodes_tx, "NodeSet") | _names(split_old["partitions"][1], "PartitionName"),
    }
    return [
        "%s: %s" % (key, why)
        for key, kinds in ENTRIES.items()
        for why in _file_refused(kinds, split_old[key], split(new[key]), scope, RENDERED[key])
    ]
