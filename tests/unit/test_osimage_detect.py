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
The image source detector and the rules over its findings: every source is a tree
built in a temporary directory, nothing is read from the network.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import functools
import http.server
import io
import json
import os
import re
import struct
import tarfile
import threading
import time

import pytest

from trixd.common.errors import BadRequest
from trixd.plugins.osimage import detect, rules

ROCKY_BASEOS_TREEINFO = """[general]
arch = x86_64
family = Rocky Linux
name = Rocky Linux 9.8
variants = AppStream,BaseOS
[tree]
arch = x86_64
variants = AppStream,BaseOS
[variant-AppStream]
repository = ../../../AppStream/x86_64/os
[variant-BaseOS]
repository = .
"""
NOBLE_RELEASE = """Origin: Ubuntu
Label: Ubuntu
Suite: noble
Version: 24.04
Codename: noble
Architectures: amd64 arm64 armhf i386 ppc64el riscv64 s390x
Components: main restricted universe multiverse
"""


def elf_header(machine: int) -> bytes:
    """
    The first 64 bytes of a little-endian ELF file for one machine.
    """
    h = bytearray(64)
    h[0:4] = b"\x7fELF"
    h[4], h[5] = 2, 1
    h[18:20] = struct.pack("<H", machine)
    return bytes(h)


def put(root, rel: str, data) -> None:
    """
    One file in the tree, directories made on the way.
    """
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def rocky_root(root, release: str = "9.8", arch: str = "x86_64") -> str:
    """
    A mirror root in the layout the books expect: BaseOS/<arch>/os and AppStream/<arch>/os.
    """
    treeinfo = ROCKY_BASEOS_TREEINFO.replace("9.8", release).replace("x86_64", arch)
    put(root, f"BaseOS/{arch}/os/.treeinfo", treeinfo)
    os.makedirs(os.path.join(str(root), f"AppStream/{arch}/os"))
    return str(root)


def tar_of(root, name: str, members: list) -> str:
    """
    A gzipped tarball of the given members, written into the tree.
    """
    p = os.path.join(str(root), name)
    with tarfile.open(p, "w:gz") as t:
        for n, data in members:
            ti = tarfile.TarInfo(n)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return p


def test_a_release_splits_into_major_and_point_release():
    """
    EL and Leap carry the point after the major; Ubuntu's YY.MM is the release itself.
    """
    assert detect.release_split("9.8") == ("9", "9.8")
    assert detect.release_split("9") == ("9", None)
    assert detect.release_split("24.04") == ("24.04", None)
    assert detect.release_split("24.04.3") == ("24.04", "24.04.3")
    assert detect.release_split("15.6") == ("15", "15.6")
    assert detect.release_split("13.7.0") == ("13", "13.7.0")


def test_distribution_words_and_arch_spellings_are_data():
    """
    A family name in any of its spellings classifies, and every arch alias normalises.
    """
    assert detect.classify("Rocky Linux")[:2] == ("rocky", "redhat")
    assert detect.classify("openSUSE Leap")[:2] == ("opensuse-leap", "suse")
    assert detect.classify("Debian GNU/Linux")[:2] == ("debian", "debian")
    assert detect.norm_arch("arm64") == "aarch64"
    assert detect.norm_arch("amd64") == "x86_64"
    assert detect.norm_arch("mips64el") is None
    assert detect.elf_arch(elf_header(243)) == "riscv64"


def test_lunas_architecture_words_each_name_an_arch_the_books_know_and_openpower_is_refused():
    """
    The osimage record spells its architecture as the DHCP client classes do, x86, arm64
    and openpower: each normalises to a row of the arch table, and openpower is refused
    with that row's sentence, since no client is published for it.
    """
    for word in ("x86", "arm64", "openpower"):
        assert detect.norm_arch(word) in rules.ARCHES, word
    assert detect.norm_arch("x86") == "x86_64" and detect.norm_arch("openpower") == "ppc64le"
    body = {"image": "rocky9", "request_id": "1761818733.1234567891234", "path": "/trinity/images/rocky9",
            "distribution": "redhat", "osrelease": "9.6", "architecture": "openpower"}
    with pytest.raises(BadRequest, match=rules.ARCHES["ppc64le"]["refusal"]):
        rules.check_create(body)
    assert rules.check_create(dict(body, architecture="x86")) == "x86_64"
    assert rules.check_create(dict(body, architecture="arm64")) == "aarch64"
    with pytest.raises(BadRequest, match="architecture mips64el is not one of aarch64, ppc64le"):
        rules.check_create(dict(body, architecture="mips64el"))
    with pytest.raises(BadRequest, match="create is reserved"):
        rules.check_create(dict(body, image="create"))
    with pytest.raises(BadRequest, match="image must match"):
        rules.check_create(dict(body, image="Rocky9"))
    with pytest.raises(BadRequest, match="media item 1 is not a URL"):
        rules.check_create(dict(body, architecture="x86", media=["ftp://mirror/rocky"]))


def test_the_mirrors_map_names_the_sources_of_a_record_and_the_arch_entry_wins():
    """
    The entry for the record's architecture, in any spelling of it, is taken before the
    one for the distribution and release alone, and a record with neither gets none;
    a key that is not the record's fields, an arch nobody spells, or a value the create
    would refuse as media is refused with the key named.
    """
    mirrors = {"redhat/9.6/x86": ["https://mirror.invalid/rocky/9/x86/"],
               "redhat/9.6": ["https://mirror.invalid/rocky/9/"],
               "ubuntu/24.04/aarch64": ["https://ports.invalid/ubuntu/"]}
    rules.check_mirrors(mirrors)
    assert rules.sources_for(mirrors, "redhat", "9.6", "x86_64") == ["https://mirror.invalid/rocky/9/x86/"]
    assert rules.sources_for(mirrors, "redhat", "9.6", "aarch64") == ["https://mirror.invalid/rocky/9/"]
    assert rules.sources_for(mirrors, "ubuntu", "24.04", "aarch64") == ["https://ports.invalid/ubuntu/"]
    assert rules.sources_for(mirrors, "ubuntu", "24.04", "x86_64") is None
    assert rules.sources_for(mirrors, "redhat", "9.5", "x86_64") is None
    assert rules.sources_for({}, "redhat", "9.6", "x86_64") is None
    for held, said in ((["redhat/9.6"], "mirrors must be a map"),
                       ({"Rocky 9": ["/isos/x.iso"]}, "mirrors key 'Rocky 9' is not"),
                       ({"redhat/9.6/mips64el": ["/isos/x.iso"]}, "mirrors key 'redhat/9.6/mips64el' is not"),
                       ({"redhat/9.6/x86/more": ["/isos/x.iso"]}, "mirrors key 'redhat/9.6/x86/more' is not"),
                       ({"redhat/9.6": "/isos/x.iso"}, "mirrors entry redhat/9.6: media must be a list"),
                       ({"redhat/9.6": ["ftp://x"]}, "mirrors entry redhat/9.6: media item 1 is not a URL")):
        with pytest.raises(BadRequest, match=re.escape(said)):
            rules.check_mirrors(held)


def test_the_record_is_held_against_what_the_sources_are():
    """
    The distribution word has to be the family's and the major release the sources',
    both named in the refusal; the point release may differ.
    """
    found = {"detected": "rocky 9.8 x86_64 from 1 source", "distribution": "redhat", "release": "9"}
    rules.check_record(found, "redhat", "9.6")
    rules.check_record(found, "redhat", "9")
    with pytest.raises(BadRequest, match="the sources are rocky 9.8 x86_64 from 1 source, distribution "
                                         "redhat release 9, and the record says distribution redhat "
                                         "osrelease 8.10"):
        rules.check_record(found, "redhat", "8.10")
    with pytest.raises(BadRequest, match="the record says distribution ubuntu osrelease 9.6"):
        rules.check_record(found, "ubuntu", "9.6")


def test_without_a_source_the_books_place_what_they_hold():
    """
    A RedHat record of this controller's own release builds from its repositories with
    no variable naming a mirror or a release; Ubuntu and Leap records hand the playbook
    the release it takes as input and build from the mirror the books name; a distribution the
    books have no compute book for, a release that is not a number, and a RedHat release
    other than the controller's are refused naming what to name.
    """
    controller = {"family": "redhat", "distro": "rocky", "release": "9", "dotrelease": "9.6"}
    own = rules.placement("redhat", "9.6", "x86_64", controller, "x86_64")
    assert (own["playbook"], own["variables"], own["base"], own["sources"]) == (
        "compute-redhat.yml", {}, None, [])
    assert own["detected"] == "redhat 9.6 x86_64 from no source, this controller's own repositories"
    assert (own["distribution"], own["release"], own["dotrelease"]) == ("redhat", "9", "9.6")
    arm = rules.placement("redhat", "9", "aarch64", controller, "aarch64")
    assert arm["variables"] == {"package_arch": "aa64", "image_arch": "aarch64"}
    with pytest.raises(BadRequest, match="without a source the books build no aarch64 redhat image on "
                                         "this x86_64 controller: name a source"):
        rules.placement("redhat", "9", "aarch64", controller, "x86_64")
    ubuntu = rules.placement("ubuntu", "24.04", "x86_64", controller, "x86_64")
    assert (ubuntu["playbook"], ubuntu["variables"]) == ("compute-ubuntu.yml", {"ubuntu_version": "24"})
    assert ubuntu["detected"] == "ubuntu 24.04 x86_64 from no source, the mirror the books name"
    leap = rules.placement("opensuse", "15.6", "x86_64", controller, "x86_64")
    assert leap["playbook"] == "compute-opensuse.yml"
    assert leap["variables"] == {"opensuse_release": "leap/15.6"}
    name = "name a source in the create, or a mirrors entry for it in the settings"
    with pytest.raises(BadRequest, match=f"the books build no alma image without a source, only "
                                         f"opensuse, redhat, ubuntu: {name}"):
        rules.placement("alma", "9.6", "x86_64", controller, "x86_64")
    with pytest.raises(BadRequest, match=f"osrelease noble is not a release number the books take: {name}"):
        rules.placement("ubuntu", "noble", "x86_64", controller, "x86_64")
    with pytest.raises(BadRequest, match="without a source the books build a redhat image from this "
                                         "controller's own repositories, which are rocky 9, and the "
                                         f"record says osrelease 8.10: {name}"):
        rules.placement("redhat", "8.10", "x86_64", controller, "x86_64")
    with pytest.raises(BadRequest, match="which are ubuntu 24.04, and the record says osrelease 9.6"):
        rules.placement("redhat", "9.6", "x86_64", {"family": "debian", "distro": "ubuntu", "release": "24.04"}, "x86_64")
    with pytest.raises(BadRequest, match="which are unknown unknown"):
        rules.placement("redhat", "9.6", "x86_64", {}, "x86_64")


def test_an_el_tree_names_its_variants_relative_to_itself(tmp_path):
    """
    A BaseOS repository's .treeinfo points at AppStream three levels up.
    """
    put(tmp_path, ".treeinfo", ROCKY_BASEOS_TREEINFO)
    f, rc = detect.identify(str(tmp_path))
    assert rc == 0
    assert (f["distro"], f["release"], f["dotrelease"], f["arch"]) == ("rocky", "9", "9.8", "x86_64")
    assert f["repos"] == {"AppStream": "../../../AppStream/x86_64/os", "BaseOS": "."}


def test_a_multi_arch_archive_asks_for_the_arch(tmp_path):
    """
    An Ubuntu archive carries seven arches; with one asked for, the deb spelling is kept.
    """
    put(tmp_path, "dists/noble/Release", NOBLE_RELEASE)
    f, rc = detect.identify(str(tmp_path))
    assert rc == 2
    assert "arch" in f["ambiguous"][0]
    f, rc = detect.identify(str(tmp_path), want_arch="arm64")
    assert rc == 0
    assert (f["codename"], f["arch"], f["arch_family_name"]) == ("noble", "aarch64", "arm64")


def test_an_archive_without_the_asked_arch_says_so(tmp_path):
    """
    The arch asked for is checked against what the Release lists.
    """
    put(tmp_path, "dists/noble/Release", NOBLE_RELEASE.replace("riscv64 ", ""))
    f, rc = detect.identify(str(tmp_path), want_arch="riscv64")
    assert rc == 2
    assert "not in this archive" in f["problems"][0]


def test_a_suse_tree_is_read_from_its_products_file(tmp_path):
    """
    media.1/products names the product and release; the arch directories beside it name the arch.
    """
    put(tmp_path, "media.1/products", "/ openSUSE-Leap 15.6-1\n")
    os.makedirs(tmp_path / "x86_64")
    f, rc = detect.identify(str(tmp_path))
    assert (f["distro"], f["release"], f["dotrelease"], f["arch"]) == ("opensuse-leap", "15", "15.6", "x86_64")


def test_an_unpacked_root_filesystem_is_read_from_os_release(tmp_path):
    """
    A root filesystem directory names its distribution in os-release and its arch in bash.
    """
    put(tmp_path, "etc/os-release", 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION="24.04.3 LTS (Noble Numbat)"\n'
                                    "VERSION_CODENAME=noble\n")
    put(tmp_path, "usr/bin/bash", elf_header(183))
    f, rc = detect.identify(str(tmp_path))
    assert rc == 0
    assert (f["kind"], f["dotrelease"], f["arch"], f["bootstrap"]) == ("rootfs-directory", "24.04.3", "aarch64", "copy")


def test_a_debian_root_takes_its_point_release_from_debian_version(tmp_path):
    """
    Debian's os-release says only the major; /etc/debian_version carries the point.
    """
    put(tmp_path, "etc/os-release", 'ID=debian\nVERSION_ID="13"\nVERSION="13 (trixie)"\nVERSION_CODENAME=trixie\n')
    put(tmp_path, "etc/debian_version", "13.7\n")
    put(tmp_path, "usr/bin/bash", elf_header(62))
    f, rc = detect.identify(str(tmp_path))
    assert (f["distro"], f["release"], f["dotrelease"]) == ("debian", "13", "13.7")


def test_an_absolute_symlink_in_a_root_stays_inside_it(tmp_path):
    """
    /etc/os-release linked to /usr/lib/os-release reads the image's file, never the controller's.
    """
    put(tmp_path, "usr/lib/os-release", 'ID="rocky"\nVERSION_ID="8.10"\n')
    os.makedirs(tmp_path / "etc")
    os.symlink("/usr/lib/os-release", tmp_path / "etc/os-release")
    os.makedirs(tmp_path / "usr/bin")
    put(tmp_path, "usr/bin/bash", elf_header(183))
    os.symlink("usr/bin", tmp_path / "bin")
    f, rc = detect.identify(str(tmp_path))
    assert (f["distro"], f["dotrelease"], f["arch"]) == ("rocky", "8.10", "aarch64")


def test_a_root_filesystem_tarball_is_streamed_once(tmp_path):
    """
    The few files detection needs are kept from one pass over the archive.
    """
    p = tar_of(tmp_path, "rootfs.tar.gz", [("./etc/os-release", b'ID="rocky"\nVERSION_ID="9.8"\n'),
                                           ("./usr/bin/bash", elf_header(62))])
    f, rc = detect.identify(p)
    assert (f["kind"], f["distro"], f["dotrelease"], f["arch"]) == ("rootfs-tarball", "rocky", "9.8", "x86_64")


def test_a_shell_that_is_a_script_does_not_name_the_arch(tmp_path):
    """
    Rocky 8 ships usr/bin/ls as a script; the arch comes from the first real ELF binary.
    """
    p = tar_of(tmp_path, "r8.tar.gz", [("./etc/os-release", b'ID="rocky"\nVERSION_ID="8.10"\n'),
                                       ("./usr/bin/ls", b"#!/bin/sh\nexec coreutils --coreutils-prog=ls"),
                                       ("./usr/bin/bash", elf_header(62))])
    f, rc = detect.identify(p)
    assert (f["dotrelease"], f["arch"]) == ("8.10", "x86_64")


def test_an_oci_archive_is_read_through_its_layers(tmp_path):
    """
    index, manifest, config and the layer that carries os-release.
    """
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w:gz") as t:
        for n, data in (("etc/os-release", b'ID="rocky"\nVERSION_ID="10.2"\n'), ("usr/bin/bash", elf_header(62))):
            ti = tarfile.TarInfo(n)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    cfg = json.dumps({"architecture": "amd64", "os": "linux"}).encode()
    man = json.dumps({"config": {"digest": "sha256:c0"}, "layers": [{"digest": "sha256:l0"}]}).encode()
    idx = json.dumps({"manifests": [{"digest": "sha256:m0"}]}).encode()
    p = tar_of(tmp_path, "oci.tar.gz", [("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'), ("index.json", idx),
                                        ("blobs/sha256/m0", man), ("blobs/sha256/c0", cfg),
                                        ("blobs/sha256/l0", layer.getvalue())])
    f, rc = detect.identify(p)
    assert rc == 0
    assert (f["kind"], f["distro"], f["release"], f["arch"]) == ("oci-archive", "rocky", "10", "x86_64")


def test_a_tree_without_metadata_is_not_identified(tmp_path):
    """
    No guess: a tree that describes nothing answers an error naming what was looked for.
    """
    put(tmp_path, "README", "hello")
    f, rc = detect.identify(str(tmp_path))
    assert rc == 1
    assert "no self-describing metadata" in f["error"]


def test_a_repository_without_an_identity_is_an_extra(tmp_path):
    """
    EPEL is repodata alone: it is recognised as a repository, with the arch its path
    names, rather than as nothing found.
    """
    put(tmp_path / "epel/8/Everything/x86_64", "repodata/repomd.xml", "<repomd/>")
    f, rc = detect.identify(str(tmp_path / "epel/8/Everything/x86_64"))
    assert rc == 0
    assert (f["kind"], f.get("distro"), f["arch"]) == ("repo-extra", None, "x86_64")


def test_detection_stops_at_the_deadline(monkeypatch):
    """
    No request starts once the total has passed, and the finding says so rather than
    answering nothing found. Each request waits the per-request bound at most.
    """
    asked = []

    def never(request, timeout):
        asked.append(timeout)
        raise AssertionError("no request should start")

    monkeypatch.setattr(detect.urllib.request, "urlopen", never)
    deadline = detect.Deadline(seconds=0)
    f, rc = detect.identify("https://mirror.example/rocky/9/", deadline=deadline)
    assert rc == 1
    assert "detection exceeded" in f["error"]
    assert asked == []
    assert detect.Deadline(seconds=100).timeout() == detect.REQUEST_TIMEOUT
    assert detect.Deadline(seconds=1).timeout() <= 1


@pytest.fixture
def served(tmp_path):
    """
    A Rocky 9.8 mirror root, served over http on a port
    of this host's own by the standard library's file server, which lists directories
    as a mirror does. Answers the URL and the list of paths asked for.
    """
    asked = []

    class Counting(http.server.SimpleHTTPRequestHandler):
        """
        The file server, keeping each path asked and saying nothing on the terminal.
        """

        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            asked.append(self.path)
            super().do_GET()

    root = rocky_root(tmp_path / "mirror", "9.8")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                             functools.partial(Counting, directory=root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/", asked
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_a_mirror_root_over_http_costs_a_handful_of_requests(served):
    """
    A release root is asked after the root's own .treeinfo and before any apt probing,
    so a Rocky mirror root, with the arch or without, is identified in at most eight
    requests, and in a fraction of the budget. A
    real mirror cost two dozen requests and most of the budget before.
    """
    url, asked = served
    started = time.monotonic()
    found = detect.identify_all([url])[0]
    assert len(asked) <= 8, asked
    assert time.monotonic() - started < detect.TOTAL_TIMEOUT / 5
    assert (found.get("distro"), found.get("dotrelease"), found.get("arch")) == ("rocky", "9.8", "x86_64"), found
    assert found["repo_urls"]["BaseOS"] == url + "BaseOS/x86_64/os/"
    assert found["ambiguous"] == ["arch: an EL release root; took x86_64, pass arch to choose"]

    del asked[:]
    found = detect.identify_all([url], want_arch="x86_64")
    assert len(asked) <= 8, asked
    answer = rules.verdict(found, arch="x86_64")
    assert answer["detected"] == "rocky 9.8 x86_64 from 1 source"
    assert answer["variables"]["image_el_baseurl"] == url


def test_a_red_hat_mirror_names_its_release_by_what_every_distribution_provides(served):
    """
    The books default to Rocky's release packages, so a mirror of another distribution,
    Alma's for one, is seeded by the name they all provide.
    """
    url, _ = served
    answer = rules.verdict(detect.identify_all([url], want_arch="x86_64"), arch="x86_64")
    assert answer["variables"].get("image_el_release_packages") == ["system-release", "python3"]


def test_a_listing_that_fills_the_read_limit_is_no_listing(monkeypatch):
    """
    A truncated autoindex page would name some entries and not others, so it is
    read as the server listing nothing.
    """
    anchor = '<a href="python312-3.12.1.rpm">'
    page = (anchor * (detect.READ_LIMIT // len(anchor) + 1)).encode()
    reader = detect.Http("https://mirror.example/oss/", detect.Deadline())
    monkeypatch.setattr(reader, "read", lambda rel, limit=detect.READ_LIMIT: page[:limit])
    assert reader.listdir("x86_64") is None
    monkeypatch.setattr(reader, "read", lambda rel, limit=detect.READ_LIMIT: page[:4096])
    assert reader.listdir("x86_64") == ["python312-3.12.1.rpm"]


def test_the_create_body_is_refused_before_any_source_is_read():
    """
    The image name is a unit name fragment and media, when given, is one to eight URLs
    or absolute paths; each refusal names the rule or the item.
    """
    body = {"image": "rocky9", "request_id": "1761818733.1234567891234", "path": "/trinity/images/rocky9",
            "distribution": "redhat", "osrelease": "9.6", "architecture": "x86"}
    assert rules.check_create(dict(body, media=["https://mirror.example/rocky/9/", "/isos/rocky9.iso"])) \
        == "x86_64"
    for name, said in (("Rocky9", "image must match"), ("-x", "image must match"),
                       ("settings", "settings is reserved"), ("create", "create is reserved")):
        with pytest.raises(BadRequest, match=said):
            rules.check_create(dict(body, image=name))
    with pytest.raises(BadRequest, match="1 to 8 sources"):
        rules.check_create(dict(body, media=[]))
    with pytest.raises(BadRequest, match="media item 2 is not a URL"):
        rules.check_create(dict(body, media=["/isos/rocky9.iso", "ftp://mirror.example/rocky/"]))


def test_a_rocky_mirror_root_gives_the_books_their_variables(tmp_path):
    """
    The verdict names the playbook, Luna's distribution word, the release and the base
    URL the books append BaseOS/<arch>/os to.
    """
    root = rocky_root(tmp_path, "9.8")
    found = detect.identify_all([root], want_arch="x86_64")
    answer = rules.verdict(found, arch="x86_64")
    assert answer["detected"] == "rocky 9.8 x86_64 from 1 source"
    assert (answer["playbook"], answer["distribution"], answer["release"], answer["arch"]) == (
        "compute-redhat.yml", "redhat", "9", "x86_64")
    assert answer["interpreter"] == "/usr/bin/python3"
    assert answer["base"] == 0 and answer["base_variable"] == "image_el_baseurl"
    assert answer["variables"]["image_el_baseurl"] == f"file://{root}/"
    assert answer["variables"]["image_el_releasever"] == "9"


def test_a_baseos_repository_given_directly_is_turned_into_its_root(tmp_path):
    """
    A BaseOS repository names AppStream three levels up, so the mirror root is above it.
    """
    root = rocky_root(tmp_path, "9.8")
    found = detect.identify_all([f"{root}/BaseOS/x86_64/os"])
    assert rules.verdict(found)["variables"]["image_el_baseurl"] == f"file://{root}/"


def test_sources_that_disagree_are_refused_naming_the_pair(tmp_path):
    """
    Two identifying sources of different majors stop the request; two point releases of
    one major are a disc beside its updates mirror and pass.
    """
    nine, eight = rocky_root(tmp_path / "nine", "9.8"), rocky_root(tmp_path / "eight", "8.10")
    with pytest.raises(BadRequest, match=f"sources disagree: {nine} is rocky 9.8 x86_64, {eight} is rocky 8.10"):
        rules.verdict(detect.identify_all([nine, eight], want_arch="x86_64"))
    newer = rocky_root(tmp_path / "newer", "9.9")
    answer = rules.verdict(detect.identify_all([nine, newer], want_arch="x86_64"))
    assert answer["detected"] == "rocky 9.9 x86_64 from 2 sources"
    assert [s["role"] for s in answer["sources"]] == ["base", "checked"]


def test_no_identifying_source_is_refused_and_so_is_an_extra_beside_one(tmp_path):
    """
    An extra repository cannot seed a build, and the books take one base URL today.
    """
    put(tmp_path / "epel", "repodata/repomd.xml", "<repomd/>")
    with pytest.raises(BadRequest, match="no identifying source"):
        rules.verdict(detect.identify_all([str(tmp_path / "epel")]))
    root = rocky_root(tmp_path, "9.8")
    with pytest.raises(BadRequest, match=f"an extra repository is not supported yet.*{tmp_path / 'epel'}"):
        rules.verdict(detect.identify_all([root, str(tmp_path / "epel")], want_arch="x86_64"))


def test_an_ubuntu_archive_on_aarch64_gets_the_foreign_debootstrap_variables(tmp_path):
    """
    The deb world spells the arch arm64, and the books take the codename and the archive;
    the client package and the console options are the books' own.
    """
    put(tmp_path, "dists/noble/Release", NOBLE_RELEASE)
    answer = rules.verdict(detect.identify_all([str(tmp_path)], want_arch="arm64"), arch="arm64")
    assert answer["detected"] == "ubuntu 24.04 aarch64 from 1 source"
    assert (answer["playbook"], answer["distribution"]) == ("compute-ubuntu.yml", "ubuntu")
    assert answer["variables"] == {
        "package_arch": "aa64",
        "image_debootstrap_flags": "--arch=arm64 --foreign",
        "ubuntu_distribution_release": "noble",
        "ubuntu_distribution_source": f"file://{tmp_path}/",
    }
    with pytest.raises(BadRequest, match="the archive carries .*; pass arch"):
        rules.verdict(detect.identify_all([str(tmp_path)]))


def test_the_verdict_carries_nothing_the_books_hold_or_derive(tmp_path):
    """
    The console options and the client package URL are the books' own, for every arch and
    family: the tables the verdicts are built from name neither.
    """
    held = {"luna_console_kerneloptions", "luna2_client_package"}
    for arch, rows in rules.ARCHES.items():
        named = set(rows["variables"]).union(*(set(v) for v in rows["family_variables"].values()))
        assert not named & held, arch
    put(tmp_path, "dists/noble/Release", NOBLE_RELEASE)
    answer = rules.verdict(detect.identify_all([str(tmp_path)], want_arch="arm64"), arch="arm64")
    assert not set(answer["variables"]) & held


def test_a_variable_the_books_need_and_nothing_gives_is_refused_naming_it():
    """
    An archive that names no codename, with no suite in the request, leaves the books
    without ubuntu_distribution_release: the build is refused, not started with a guess.
    """
    archive = detect.Finding(source="https://archive.example/ubuntu/", kind="repository",
                             family="debian", distro="ubuntu", release="24", dotrelease="24.04",
                             arch="x86_64")
    with pytest.raises(BadRequest, match="the books need ubuntu_distribution_release"):
        rules.verdict([archive])
    assert rules.verdict([archive], suite="noble")["variables"]["ubuntu_distribution_release"] == "noble"


def test_a_leap_build_takes_an_http_mirror_and_refuses_a_disc_or_a_local_tree(tmp_path):
    """
    Only an http mirror was ever built for Leap: a disc or a file:// tree, alone or
    beside the mirror, is refused naming it, and the mirror gives the books their
    variables.
    """
    for name in ("oss", "dvd"):
        put(tmp_path / name, "media.1/products", "/ openSUSE-Leap 15.6-1\n")
        os.makedirs(tmp_path / name / "x86_64")
    disc, tree = str(tmp_path / "dvd"), str(tmp_path / "oss")
    found = detect.identify_all([disc, tree])
    found[0]["kind"] = "iso"
    refusal = ("a Leap build from a disc or a file:// tree is not proven yet, only an http "
               "mirror was: name the OSS repository URL")
    with pytest.raises(BadRequest, match=f"{re.escape(refusal)}: {re.escape(disc)}"):
        rules.verdict(found)
    with pytest.raises(BadRequest, match=f"{re.escape(refusal)}: {re.escape(tree)}"):
        rules.verdict(found[1:])
    mirror = detect.Finding(source="https://download.opensuse.org/distribution/leap/15.6/repo/oss/",
                            kind="repository", family="suse", distro="opensuse-leap", release="15",
                            dotrelease="15.6", arch="x86_64")
    with pytest.raises(BadRequest, match=f"{re.escape(refusal)}: {re.escape(disc)}"):
        rules.verdict([mirror, found[0]])
    answer = rules.verdict([mirror])
    assert answer["base"] == 0
    assert answer["variables"]["opensuse_repo_baseurl"] == mirror["source"]
    assert answer["variables"]["opensuse_release"] == "leap/15.6"


def test_each_refused_kind_is_refused_in_one_sentence(tmp_path):
    """
    A root filesystem seed, a remote ISO, an Ubuntu disc and riscv64 each name why.
    """
    put(tmp_path / "rootfs", "etc/os-release", 'ID=rocky\nVERSION_ID="9.8"\n')
    put(tmp_path / "rootfs", "usr/bin/bash", elf_header(62))
    with pytest.raises(BadRequest, match="a root filesystem seed is not supported yet"):
        rules.verdict(detect.identify_all([str(tmp_path / "rootfs")]))
    remote = detect.Finding(source="https://mirror.example/rocky9.iso", kind="iso", distro="rocky",
                            family="redhat", release="9", arch="x86_64")
    with pytest.raises(BadRequest, match="a remote ISO is not supported yet"):
        rules.verdict([remote])
    put(tmp_path / "ubuntu", "dists/noble/Release", NOBLE_RELEASE)
    disc = detect.identify_all([str(tmp_path / "ubuntu")], want_arch="amd64")
    disc[0]["kind"] = "iso"
    with pytest.raises(BadRequest, match="a disc cannot feed debootstrap: name the archive URL"):
        rules.verdict(disc)
    with pytest.raises(BadRequest, match="no luna2-client is published for riscv64"):
        rules.verdict(detect.identify_all([str(tmp_path / "ubuntu")], want_arch="riscv64"), arch="riscv64")
