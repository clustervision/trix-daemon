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
What may be built from a set of sources, as data and one verdict: the create body and the
mirrors map a build's sources come from, the families and arches the books build, the
Python 3.6 rule, what the books build with no source at all, and the refusals, each one
sentence naming what was refused. A refusal is a BadRequest: a caller answers it as it
stands, and the runner writes it as the reason the build failed.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import posixpath
import re
import urllib.parse

from trixd.common.errors import BadRequest
from trixd.plugins.osimage import detect

# A job name is a directory name, so narrower than Luna's own image name rule. settings and
# create are routes of this plugin's own.
NAME_RULE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
RESERVED_NAMES: tuple = ("settings", "create")
MEDIA_RULE = re.compile(r"""^(https?|file)://[^\s'"]+$|^/[^\s'"]+$""")
MEDIA_MOST: int = 8
# A key of the mirrors map: the record's distribution and release as the create schema
# takes them, and the architecture in any of its spellings when the entry is for one.
MIRROR_KEY = re.compile(r"^([a-z0-9._-]{1,32})/([A-Za-z0-9._+-]{1,32})(?:/([a-z0-9_]{2,16}))?$")

# Per family: the playbook, the word Luna's record takes as its distribution, and the
# variable the books read the base URL from. The playbook comes from the family; what
# was proven from each is rocky, ubuntu and opensuse-leap.
FAMILIES = {
    "redhat": {"playbook": "compute-redhat.yml", "distribution": "redhat", "base": "image_el_baseurl"},
    "debian": {"playbook": "compute-ubuntu.yml", "distribution": "ubuntu",
               "base": "ubuntu_distribution_source"},
    "suse": {"playbook": "compute-opensuse.yml", "distribution": "opensuse",
             "base": "opensuse_repo_baseurl"},
}

# Per arch: whether a build is offered, and the book variables of a foreign arch, by family
# where they spell it differently. riscv64 and ppc64le are refused until a client package exists.
ARCHES = {
    "x86_64": {"enabled": True, "variables": {}, "family_variables": {}, "skip_tags": []},
    "aarch64": {"enabled": True, "variables": {"package_arch": "aa64"},
                "family_variables": {"redhat": {"image_arch": "aarch64"},
                                     "debian": {"image_debootstrap_flags": "--arch=arm64 --foreign"}},
                "skip_tags": []},
    "riscv64": {"enabled": False, "refusal": "no luna2-client is published for riscv64",
                "variables": {},
                "family_variables": {"debian": {"image_debootstrap_flags": "--arch=riscv64 --foreign"}},
                "skip_tags": ["monitoring", "slurm"]},
    "ppc64le": {"enabled": False, "refusal": "no luna2-client is published for ppc64le",
                "variables": {}, "family_variables": {}, "skip_tags": []},
}

# Releases whose own Python is 3.6, which ansible-core cannot drive: the image gets the
# newer interpreter named here from its own sources, and the build is refused when no
# source carries it. Keyed by family and major release.
PYTHON36 = {
    ("redhat", "8"): {"package": "python3.12", "where": "add the DVD ISO or the AppStream URL"},
    ("suse", "15"): {"package": "python312", "where": "add the OSS repository URL"},
}
INTERPRETER: str = "/usr/bin/python3"
INTERPRETER_PYTHON36: str = "/usr/bin/python3.12"
PACKAGES: tuple = tuple(sorted({rule["package"] for rule in PYTHON36.values()}))

# What a source that identifies a distribution but cannot seed a build is told.
SEED_ONLY: tuple = ("rootfs-tarball", "oci-archive", "rootfs-directory")
REFUSALS = {
    "seed": "a root filesystem seed is not supported yet: name a repository URL or a DVD ISO",
    "remote_iso": "a remote ISO is not supported yet: copy it to the controller and name the file",
    "extra": "an extra repository is not supported yet, the books take one base URL",
    "debian_iso": "a disc cannot feed debootstrap: name the archive URL",
    "suse_local": "a Leap build from a disc or a file:// tree is not proven yet, only an http "
                  "mirror was: name the OSS repository URL",
    "no_identity": "no identifying source: name a repository URL or a DVD ISO "
                   "(.treeinfo, dists/<suite>/Release or media.1/products)",
    "no_arch": "no source names an arch: pass arch",
    "no_root": "name the mirror root, the directory holding BaseOS and AppStream",
    "no_source": "name a source in the create, or a mirrors entry for it in the settings",
}
# Where the books build from with no source: a RedHat image from the controller's own
# repositories, so only the controller's own release; Ubuntu and Leap from the mirror they name.
OWN_REPOSITORIES: str = "this controller's own repositories"
BOOK_MIRROR: str = "the mirror the books name"


def check_name(name: str, field: str = "name") -> None:
    """
    Refuse a name no job can be made from, naming the field it came in.
    """
    if not isinstance(name, str) or not NAME_RULE.match(name):
        raise BadRequest(f"{field} must match {NAME_RULE.pattern}")
    if name in RESERVED_NAMES:
        raise BadRequest(f"{name} is reserved")


def check_create(body: dict) -> str:
    """
    Refuse a create body before anything is written, and answer the arch the books spell:
    Luna names the record's architecture in its own words.
    """
    check_name(body["image"], "image")
    arch = detect.norm_arch(body["architecture"])
    offered = ARCHES.get(arch)
    if not offered:
        raise BadRequest(f"architecture {body['architecture']} is not one of "
                         f"{', '.join(sorted(ARCHES))}")
    if not offered["enabled"]:
        raise BadRequest(offered["refusal"])
    if body.get("media") is not None:
        check_media(body["media"])
    return arch


def check_media(media) -> None:
    """
    Refuse a list of sources that is not one to eight URLs or absolute paths.
    """
    if not isinstance(media, list) or not 1 <= len(media) <= MEDIA_MOST:
        raise BadRequest(f"media must be a list of 1 to {MEDIA_MOST} sources")
    for position, item in enumerate(media, 1):
        if not isinstance(item, str) or not MEDIA_RULE.match(item):
            raise BadRequest(f"media item {position} is not a URL (http, https or file) "
                             f"or an absolute path: {item!r}")


def check_mirrors(mirrors) -> None:
    """
    Refuse a mirrors map whose keys are not distribution/osrelease[/architecture] or
    whose values are not lists of sources.
    """
    if not isinstance(mirrors, dict):
        raise BadRequest("mirrors must be a map of distribution/osrelease[/architecture] "
                         "to a list of sources")
    for key, media in mirrors.items():
        found = MIRROR_KEY.match(key)
        if not found or (found.group(3) and detect.norm_arch(found.group(3)) is None):
            raise BadRequest(f"mirrors key {key!r} is not <distribution>/<osrelease> or "
                             f"<distribution>/<osrelease>/<architecture>")
        try:
            check_media(media)
        except BadRequest as error:
            raise BadRequest(f"mirrors entry {key}: {error.message}") from None


def sources_for(mirrors: dict, distribution: str, osrelease: str, arch: str):
    """
    The sources the mirrors map names for a record: the entry for its architecture, else
    the one for the distribution and release alone, else none.
    """
    keyed = {}
    for key, media in mirrors.items():
        found = MIRROR_KEY.match(key)
        keyed[(found.group(1), found.group(2), detect.norm_arch(found.group(3)))] = media
    return keyed.get((distribution, osrelease, arch)) or keyed.get((distribution, osrelease, None))


def check_record(found: dict, distribution: str, osrelease: str) -> None:
    """
    Refuse sources that are not what the record says: the distribution word has to be
    the family's, and the major release the sources'. A point release may differ, as a
    mirror that moved on from the record does.
    """
    major = detect.release_split(osrelease)[0]
    if found["distribution"] != distribution or found["release"] != major:
        raise BadRequest(f"the sources are {found['detected']}, distribution "
                         f"{found['distribution']} release {found['release']}, and the record "
                         f"says distribution {distribution} osrelease {osrelease}")


def placement(distribution: str, osrelease: str, arch: str, controller: dict) -> dict:
    """
    What the books build with no source, for a record's distribution and release, as a
    verdict in the shape detection gives: the playbook, the arch's variables, and the
    release where the playbook takes one as input. A RedHat image is the controller's
    own release or nothing, and a distribution or release that cannot be placed is a
    BadRequest naming what to name.
    """
    families = {rule["distribution"]: family for family, rule in FAMILIES.items()}
    family = families.get(distribution)
    if not family:
        raise BadRequest(f"the books build no {distribution} image without a source, only "
                         f"{', '.join(sorted(families))}: {REFUSALS['no_source']}")
    release, dotrelease = detect.release_split(osrelease)
    if not release:
        raise BadRequest(f"osrelease {osrelease} is not a release number the books take: "
                         f"{REFUSALS['no_source']}")
    offered = ARCHES[arch]
    variables = dict(offered["variables"])
    variables.update(offered["family_variables"].get(family, {}))
    if family == "redhat":
        own = f"{controller.get('distro') or 'unknown'} {controller.get('release') or 'unknown'}"
        if controller.get("family") != "redhat" or controller.get("release") != release:
            raise BadRequest(f"without a source the books build a {distribution} image from "
                             f"{OWN_REPOSITORIES}, which are {own}, and the record says "
                             f"osrelease {osrelease}: {REFUSALS['no_source']}")
        where = OWN_REPOSITORIES
    elif family == "debian":
        variables["ubuntu_version"] = release.split(".")[0]
        where = BOOK_MIRROR
    else:
        variables["opensuse_release"] = f"leap/{dotrelease or release}"
        where = BOOK_MIRROR
    rule = PYTHON36.get((family, release))
    if rule:
        variables["image_bootstrap_extra"] = [rule["package"]]
    return {"detected": f"{distribution} {osrelease} {arch} from no source, {where}",
            "family": family, "distribution": distribution, "distro": distribution,
            "release": release, "dotrelease": dotrelease, "arch": arch,
            "python36": bool(rule),
            "interpreter": INTERPRETER_PYTHON36 if rule else INTERPRETER,
            "playbook": FAMILIES[family]["playbook"],
            "base": None, "base_variable": FAMILIES[family]["base"],
            "variables": variables, "skip_tags": list(offered["skip_tags"]),
            "notes": _check_python([], rule) if rule else [],
            "sources": []}


def verdict(findings: list, arch: str = None, suite: str = None) -> dict:
    """
    What the findings of a request's sources add up to: the family, release and arch
    the books are given, the source that seeds the build, and the variables, or a
    BadRequest naming the source or the pair that stops it.
    """
    _refuse_unreadable(findings)
    identifying = [f for f in findings if f.get("distro")]
    if not identifying:
        raise BadRequest(REFUSALS["no_identity"])
    for extra in [f for f in findings if f.get("kind") == "repo-extra"]:
        raise BadRequest(f"{REFUSALS['extra']}: {extra['source']}")
    _refuse_disagreement(identifying)
    family, distro = identifying[0]["family"], identifying[0]["distro"]
    release, dotrelease = identifying[0]["release"], _point_release(identifying)
    arch = _arch_of(identifying, detect.norm_arch(arch) if arch else None)
    base = _base_of(findings, identifying, family)
    rule = PYTHON36.get((family, release))
    notes = _check_python(findings, rule) if rule else []

    variables = dict(ARCHES[arch]["variables"])
    variables.update(ARCHES[arch]["family_variables"].get(family, {}))
    variables.update(_family_variables(family, findings[base], release, dotrelease, suite))
    if rule:
        # The books hold the hook and an empty default, not which release needs a newer Python.
        variables["image_bootstrap_extra"] = [rule["package"]]
    for key, value in variables.items():
        if value in (None, ""):
            raise BadRequest(f"the books need {key} and neither the request nor the sources give it")
    count = f"{len(findings)} source" + ("s" if len(findings) > 1 else "")
    return {"detected": f"{distro} {dotrelease or release} {arch} from {count}",
            "family": family, "distribution": FAMILIES[family]["distribution"], "distro": distro,
            "release": release, "dotrelease": dotrelease, "arch": arch,
            "python36": bool(rule),
            "interpreter": INTERPRETER_PYTHON36 if rule else INTERPRETER,
            "playbook": FAMILIES[family]["playbook"],
            "base": base, "base_variable": FAMILIES[family]["base"],
            "variables": variables, "skip_tags": list(ARCHES[arch]["skip_tags"]),
            "notes": notes,
            "sources": [{"source": f["source"], "kind": f.get("kind"),
                         "role": "base" if number == base else "checked"}
                        for number, f in enumerate(findings)]}


def _refuse_unreadable(findings: list) -> None:
    """
    A source that could not be read, or asks the operator something, stops the lot.
    """
    for f in findings:
        if f.get("error"):
            raise BadRequest(f"{f['source']}: {f['error']}")
        for sentence in f.get("problems", []) + f.get("ambiguous", []):
            raise BadRequest(f"{f['source']}: {sentence}")
        if f.get("kind") in SEED_ONLY:
            raise BadRequest(f"{REFUSALS['seed']}: {f['source']}")
        if f.get("kind") == "iso" and re.match(r"^[a-z]+://", f["source"]):
            raise BadRequest(f"{REFUSALS['remote_iso']}: {f['source']}")


def _refuse_disagreement(identifying: list) -> None:
    """
    Every identifying source says the same distribution, major release and arch; the
    point release may differ, as a disc and the updates mirror beside it do.
    """
    first = identifying[0]
    for other in identifying[1:]:
        same = (first["distro"] == other["distro"] and first["release"] == other["release"]
                and (not first.get("arch") or not other.get("arch") or first["arch"] == other["arch"]))
        if not same:
            raise BadRequest(f"sources disagree: {first['source']} is {_identity(first)}, "
                             f"{other['source']} is {_identity(other)}")


def _identity(f: dict) -> str:
    """
    One source in the words the disagreement sentence uses.
    """
    return " ".join(str(part) for part in (f["distro"], f.get("dotrelease") or f["release"],
                                           f.get("arch") or "any arch"))


def _point_release(identifying: list) -> str:
    """
    The newest point release any source names, or none when none does.
    """
    named = [f["dotrelease"] for f in identifying if f.get("dotrelease")]
    return max(named, key=lambda v: [int(n) for n in v.split(".")]) if named else None


def _arch_of(identifying: list, wanted: str) -> str:
    """
    The one arch the build is for, from the request or from the sources, and one the
    books offer.
    """
    known = {f["arch"] for f in identifying if f.get("arch")}
    arch = wanted or (known.pop() if known else None)
    if not arch:
        raise BadRequest(REFUSALS["no_arch"])
    offered = ARCHES.get(arch)
    if not offered:
        raise BadRequest(f"arch {arch} is not one of {', '.join(sorted(ARCHES))}")
    if not offered["enabled"]:
        raise BadRequest(offered["refusal"])
    return arch


def _base_of(findings: list, identifying: list, family: str) -> int:
    """
    The index of the source the books are given as the base URL: a disc seeds a RedHat
    build, a mirror every other, and the family's own refusals for the rest. A Leap
    source that is not an http mirror is refused: the image's own repository file is
    written from the base URL, and a tree the chroot cannot reach was never built.
    """
    discs = [f for f in identifying if f.get("kind") == "iso"]
    mirrors = [f for f in identifying if f.get("kind") != "iso"]
    if family == "debian" and discs:
        raise BadRequest(f"{REFUSALS['debian_iso']}: {discs[0]['source']}")
    if family == "suse":
        local = [f for f in identifying if f in discs or not re.match(r"^https?://", f["source"])]
        if local:
            raise BadRequest(f"{REFUSALS['suse_local']}: {local[0]['source']}")
    chosen = (discs or mirrors)[0] if family == "redhat" else mirrors[0]
    return findings.index(chosen)


def _check_python(findings: list, rule: dict) -> list:
    """
    A Python 3.6 image needs the newer interpreter in a source. Refused on evidence
    only: a source whose package directories could not be listed is a note, not a
    refusal, and the build itself then says.
    """
    checked = [f["packages"].get(rule["package"]) for f in findings if "packages" in f]
    if any(checked):
        return []
    if checked:
        raise BadRequest(f"a Python 3.6 image needs {rule['package']} in a source: {rule['where']}")
    return [f"{rule['package']} could not be checked: no source lists its packages"]


def _family_variables(family: str, base: dict, release: str, dotrelease: str, suite: str) -> dict:
    """
    What only the sources know: the release they carry and where the base is. The base
    URL of a disc is left to the runner, which mounts it. Everything else the books hold.
    """
    variables = {}
    if family == "redhat":
        variables["image_el_releasever"] = release
        if base.get("kind") != "iso":
            variables["image_el_baseurl"] = _mirror_root(base)
    elif family == "debian":
        variables["ubuntu_distribution_release"] = base.get("codename") or suite or base.get("suite")
        variables["ubuntu_distribution_source"] = _location(base["source"])
    elif family == "suse":
        variables["opensuse_release"] = f"leap/{dotrelease or release}"
        variables["suse_version"] = dotrelease or release
        variables["opensuse_repo_baseurl"] = _location(base["source"])
        variables["enable_slurm"] = True
    return variables


def _location(source: str) -> str:
    """
    A source as a URL: a local directory as file://, a URL as given, ending in a slash.
    """
    url = source if re.match(r"^[a-z]+://", source) else "file://" + os.path.abspath(source)
    return url if url.endswith("/") else url + "/"


def _mirror_root(base: dict) -> str:
    """
    The URL the books append BaseOS/<arch>/os to: the source when it is that root, or
    the root above a BaseOS repository given directly.
    """
    repos = base.get("repos") or {}
    if "BaseOS" not in repos:
        raise BadRequest(f"{REFUSALS['no_root']}: {base['source']}")
    location = _location(base["source"])
    where = urllib.parse.urlsplit(urllib.parse.urljoin(location, repos["BaseOS"].rstrip("/") + "/"))
    tail = f"/BaseOS/{base.get('arch')}/os/"
    if not where.path.endswith(tail):
        raise BadRequest(f"{REFUSALS['no_root']}: {base['source']}")
    root = posixpath.normpath(where.path[:-len(tail)] or "/")
    return urllib.parse.urlunsplit((where.scheme, where.netloc, root.rstrip("/") + "/", "", ""))
