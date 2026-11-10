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
What an image source is: family, distribution, release, point release, codename and
arch, read from the source's own metadata before a build is queued.

A source is a package repository URL (an RPM tree with .treeinfo or repodata, a Debian or
Ubuntu archive root with dists/<suite>/Release, an openSUSE tree with media.1/products),
an ISO image read in place (ISO 9660 with Rock Ridge or Joliet; a URL costs a few range
requests, nothing is downloaded whole), a local directory (a mounted disc, a mirror or an
unpacked root filesystem), or a root filesystem tarball. Standard library only, no root,
no mount. Every finding carries its evidence, which file and which field, and reports
ambiguity rather than guessing. The rules that decide what may be built from a set of
findings are in trixd.plugins.osimage.rules.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import configparser
import json
import os
import posixpath
import re
import struct
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

# The network bound of one detection: no single request waits longer than the first, and
# no request starts once the second has passed, so a mirror that never answers costs a
# build a bounded wait and one sentence, not an hour.
REQUEST_TIMEOUT: int = 5
TOTAL_TIMEOUT: int = 30

# The most a file or a directory listing is read; a listing that fills it is incomplete
# and is reported as no listing at all, never as a partial one.
READ_LIMIT: int = 4 << 20

# arch vocabulary: one canonical name (the kernel's uname -m) per arch, family spellings are data
ARCH_ALIASES = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64", "x86": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64", "aa64": "aarch64",
    "riscv64": "riscv64",
    "ppc64le": "ppc64le", "ppc64el": "ppc64le", "openpower": "ppc64le",
    "s390x": "s390x",
}
DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "riscv64": "riscv64", "ppc64le": "ppc64el", "s390x": "s390x"}
ELF_MACHINE = {62: "x86_64", 183: "aarch64", 243: "riscv64", 21: "ppc64le", 22: "s390x"}

# distro vocabulary: os-release ID or treeinfo family, to (distro, family, bootstrap method)
DISTROS = [
    # (regex on ID or family/name, distro id, family, bootstrap)
    (r"^rocky", "rocky", "redhat", "dnf-installroot"),
    (r"^(rhel|red ?hat)", "rhel", "redhat", "dnf-installroot"),
    (r"^alma", "almalinux", "redhat", "dnf-installroot"),
    (r"^centos", "centos", "redhat", "dnf-installroot"),
    (r"^(ol|oracle)", "oraclelinux", "redhat", "dnf-installroot"),
    (r"^fedora", "fedora", "redhat", "dnf-installroot"),
    (r"^ubuntu", "ubuntu", "debian", "debootstrap"),
    (r"^debian", "debian", "debian", "debootstrap"),
    (r"^(opensuse[- ]?leap|opensuse$)", "opensuse-leap", "suse", "zypper-root"),
    (r"^opensuse[- ]?tumbleweed", "opensuse-tumbleweed", "suse", "zypper-root"),
    (r"^(sles|suse linux enterprise)", "sles", "suse", "zypper-root"),
]

# The arches an EL release root may carry under BaseOS, in the order one is taken.
EL_ARCHES: tuple = ("x86_64", "aarch64", "riscv64", "ppc64le", "s390x")

class Expired(Exception):
    """
    The detection deadline passed with reads still to do.
    """


class Deadline:
    """
    The time one detection may spend on the network, shared by every reader of a request.
    """

    def __init__(self, seconds: float = TOTAL_TIMEOUT) -> None:
        """
        Start the clock.
        """
        self.seconds = seconds
        self.ends = time.monotonic() + seconds

    def timeout(self) -> float:
        """
        How long the next request may wait, or Expired once nothing may start.
        """
        left = self.ends - time.monotonic()
        if left <= 0:
            raise Expired("detection exceeded %g s" % self.seconds)
        return min(REQUEST_TIMEOUT, left)


def classify(text):
    t = (text or "").strip().lower()
    for rx, distro, family, boot in DISTROS:
        if re.search(rx, t):
            return distro, family, boot
    return None, None, None


def norm_arch(a):
    return ARCH_ALIASES.get((a or "").strip().lower())


# readers: one interface (read(path) gives bytes or None, listdir(path) gives names or None) over every source kind
class Http:
    """Random access and small-file reads over HTTP(S)/FTP/file URLs."""

    def __init__(self, base, deadline=None):
        self.base = base if base.endswith("/") else base + "/"
        self.deadline = deadline or Deadline()

    def url(self, rel):
        return urllib.parse.urljoin(self.base, rel)

    def read(self, rel, limit=READ_LIMIT):
        timeout = self.deadline.timeout()
        try:
            with urllib.request.urlopen(self.url(rel), timeout=timeout) as r:
                return r.read(limit)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def listdir(self, rel):
        """Directory names from an autoindex page (Apache/nginx); None when the server lists nothing."""
        data = self.read(rel if rel.endswith("/") else rel + "/")
        if not data or len(data) >= READ_LIMIT:
            return None
        names = re.findall(r'href="([^"?/][^"?]*?)/?"', data.decode("utf-8", "replace"))
        return sorted({urllib.parse.unquote(n).rstrip("/") for n in names if not n.startswith(("http", "/", "."))})


class LocalDir:
    """A tree on disk, read as a root: symlinks resolve inside it (an image's absolute link /etc/os-release to
    /usr/lib/os-release must not read the controller's own file)."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _resolve(self, rel, hops=40):
        parts, done = [p for p in rel.split("/") if p not in ("", ".")], []
        while parts:
            name = parts.pop(0)
            if name == "..":
                done = done[:-1]
                continue
            here = os.path.join(self.root, *done, name)
            if os.path.islink(here):
                hops -= 1
                if hops < 0:
                    return None
                target = os.readlink(here)
                if target.startswith("/"):
                    done = []
                parts = [p for p in target.split("/") if p not in ("", ".")] + parts
                continue
            done.append(name)
        return os.path.join(self.root, *done)

    def read(self, rel, limit=READ_LIMIT):
        p = self._resolve(rel)
        if p is None:
            return None
        try:
            with open(p, "rb") as f:
                return f.read(limit)
        except OSError:
            return None

    def listdir(self, rel):
        p = self._resolve(rel)
        if p is None:
            return None
        try:
            return sorted(os.listdir(p))
        except OSError:
            return None


class RangeFile:
    """A read-only, seekable view of a remote file through HTTP range requests (block cache)."""
    BLOCK = 64 << 10

    def __init__(self, url, deadline=None):
        self.url, self.cache = url, {}
        self.deadline = deadline or Deadline()

    def _block(self, n):
        if n not in self.cache:
            req = urllib.request.Request(self.url, headers={"Range": "bytes=%d-%d" % (n * self.BLOCK, (n + 1) * self.BLOCK - 1)})
            with urllib.request.urlopen(req, timeout=self.deadline.timeout()) as r:
                if r.status != 206:
                    raise OSError("server ignores range requests (HTTP %s): cannot read the ISO in place" % r.status)
                self.cache[n] = r.read()
        return self.cache[n]

    def pread(self, off, size):
        out = bytearray()
        while size > 0:
            b = self._block(off // self.BLOCK)
            chunk = b[off % self.BLOCK:off % self.BLOCK + size]
            if not chunk:
                break
            out += chunk
            off += len(chunk)
            size -= len(chunk)
        return bytes(out)


class LocalFile:
    def __init__(self, path):
        self.f = open(path, "rb")

    def pread(self, off, size):
        self.f.seek(off)
        return self.f.read(size)


class Iso:
    """Minimal ISO 9660 reader: Joliet names when present, else Rock Ridge NM, else plain 9660 (no ;1)."""
    SECTOR = 2048

    def __init__(self, blob):
        self.blob = blob
        self.root = None
        self.joliet = False
        self.volume_id = ""
        primary = None
        for i in range(16, 64):
            vd = blob.pread(i * self.SECTOR, self.SECTOR)
            if len(vd) < 7 or vd[1:6] != b"CD001":
                raise ValueError("not an ISO 9660 image")
            kind = vd[0]
            if kind == 255:
                break
            if kind == 1 and primary is None:
                primary = vd
                self.volume_id = vd[40:72].decode("ascii", "replace").strip()
            if kind == 2 and vd[88:91] in (b"%/@", b"%/C", b"%/E"):   # Joliet supplementary descriptor
                self.root, self.joliet = self._dirrec(vd[156:190]), True
        if self.root is None:
            if primary is None:
                raise ValueError("ISO without a primary volume descriptor")
            self.root = self._dirrec(primary[156:190])

    @staticmethod
    def _dirrec(rec):
        return {"extent": struct.unpack("<I", rec[2:6])[0], "size": struct.unpack("<I", rec[10:14])[0],
                "dir": bool(rec[25] & 2)}

    def _entries(self, d):
        data = self.blob.pread(d["extent"] * self.SECTOR, d["size"])
        off, out = 0, {}
        while off < len(data):
            ln = data[off]
            if ln == 0:                                   # records never cross a sector: skip to the next one
                off = (off // self.SECTOR + 1) * self.SECTOR
                continue
            rec = data[off:off + ln]
            nlen = rec[32]
            raw = rec[33:33 + nlen]
            if raw not in (b"\x00", b"\x01"):
                if self.joliet:
                    name = raw.decode("utf-16-be", "replace")
                else:
                    name = self._rr_name(rec, 33 + nlen + (1 - nlen % 2)) or raw.decode("ascii", "replace")
                name = name.split(";")[0]
                if not self.joliet and name.endswith(".") and "." not in name[:-1]:
                    name = name[:-1]
                out[name] = self._dirrec(rec)
            off += ln
        return out

    @staticmethod
    def _rr_name(rec, su):
        parts, i = [], su
        while i + 4 <= len(rec):
            sig, ln = rec[i:i + 2], rec[i + 2]
            if ln < 4:
                break
            if sig == b"NM":
                parts.append(rec[i + 5:i + ln].decode("utf-8", "replace"))
            i += ln
        return "".join(parts) or None

    def _lookup(self, rel):
        node = self.root
        for part in [p for p in rel.split("/") if p]:
            if not node["dir"]:
                return None
            ents = self._entries(node)
            hit = ents.get(part)
            if hit is None:                                # plain 9660 is upper case; tolerate any case
                hit = {k.lower(): v for k, v in ents.items()}.get(part.lower())
            if hit is None:
                return None
            node = hit
        return node

    def read(self, rel, limit=READ_LIMIT):
        n = self._lookup(rel)
        if not n or n["dir"]:
            return None
        return self.blob.pread(n["extent"] * self.SECTOR, min(n["size"], limit))

    def listdir(self, rel):
        n = self._lookup(rel)
        return sorted(self._entries(n)) if n and n["dir"] else None


class Tarball:
    """Read named members of a rootfs tarball (stream once, keep the few files detection needs)."""
    WANT = ("etc/os-release", "usr/lib/os-release", "etc/debian_version", "etc/redhat-release", "etc/SuSE-release")

    def __init__(self, path):
        self.files, self.elf, self.oci = {}, None, None
        small = {}
        with tarfile.open(path, "r:*") as t:
            self._scan(t, small)
        if "oci-layout" in small and "index.json" in small:     # an OCI image layout archive (container base image)
            self._oci(path, small)

    def _scan(self, t, small):
        for m in t:
            name = m.name.lstrip("./")
            if m.isfile() and name in self.WANT:
                self.files[name] = t.extractfile(m).read(1 << 20)
            elif m.isfile() and self.elf is None and re.match(r"^(usr/)?bin/(bash|sh|busybox|ls)$", name):
                head = t.extractfile(m).read(64)
                if head[:4] == b"\x7fELF":                  # e.g. Rocky 8 ships usr/bin/ls as a 49-byte script
                    self.elf = head
            elif m.issym() and name in self.WANT and name not in self.files:
                self.files.setdefault("@link:" + name, m.linkname.encode())
            elif small is not None and m.isfile() and m.size < (64 << 10) and (
                    name in ("oci-layout", "index.json") or name.startswith("blobs/")):
                small[name] = t.extractfile(m).read()

    def _oci(self, path, small):
        """index, then manifest, then config (architecture) and layers; os-release is read from the layers, newest first."""
        blob = lambda d: small.get("blobs/" + d.replace(":", "/"))
        index = json.loads(small["index.json"])
        man = json.loads(blob(index["manifests"][0]["digest"]) or b"{}")
        if "manifests" in man:                                 # an image index: take the first platform manifest
            man = json.loads(blob(man["manifests"][0]["digest"]) or b"{}")
        cfg = json.loads(blob(man.get("config", {}).get("digest", "")) or b"{}")
        self.oci = {"architecture": cfg.get("architecture"), "os": cfg.get("os"),
                    "layers": [l["digest"] for l in man.get("layers", [])]}
        wanted = {"blobs/" + d.replace(":", "/") for d in self.oci["layers"]}
        with tarfile.open(path, "r:*") as t:
            for m in t:
                if m.name.lstrip("./") in wanted:
                    with tarfile.open(fileobj=t.extractfile(m), mode="r|*") as layer:
                        self._scan(layer, None)

    def read(self, rel, limit=READ_LIMIT):
        data = self.files.get(rel)
        if data is None and ("@link:" + rel) in self.files:       # etc/os-release linked to ../usr/lib/os-release
            target = os.path.normpath(os.path.join(os.path.dirname(rel), self.files["@link:" + rel].decode()))
            data = self.files.get(target.lstrip("/"))
        return data

    def listdir(self, rel):
        return None


# the readers' interpretation
def kv_file(data):
    """os-release / Release / .disk/info style: KEY=value or Key: value lines."""
    out = {}
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*"?(.*?)"?\s*$', line)
        if m and not line.startswith(" "):
            out.setdefault(m.group(1), m.group(2))
    return out


def elf_arch(head):
    if not head or head[:4] != b"\x7fELF":
        return None
    endian = "<" if head[5] == 1 else ">"
    return ELF_MACHINE.get(struct.unpack(endian + "H", head[18:20])[0])


def release_split(version):
    """'9.8' gives ('9', '9.8'); '24.04.3' gives ('24.04', '24.04.3'); '15.6' gives ('15', '15.6'); '13.7' gives ('13', '13.7')."""
    v = (version or "").strip()
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", v)
    if not m:
        return None, None
    if len(m.group(1)) == 2 and m.group(2) and len(m.group(2)) == 2:          # Ubuntu YY.MM is the release itself
        rel = "%s.%s" % (m.group(1), m.group(2))
        return rel, (v if m.group(3) else None)
    return m.group(1), (v if m.group(2) else None)


class Finding(dict):
    def note(self, src, what):
        self.setdefault("evidence", []).append("%s: %s" % (src, what))


def from_treeinfo(r, prefix, f):
    data = r.read(prefix + ".treeinfo") or r.read(prefix + "treeinfo")
    if not data:
        return False
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(data.decode("utf-8", "replace"))
    except configparser.Error:
        return False
    g = lambda s, k: cp.get(s, k, fallback=None)
    family_txt = g("release", "name") or g("general", "family") or g("general", "name")
    version = g("release", "version") or g("general", "version")
    if not version:
        m = re.search(r"(\d+(?:\.\d+)+|\d+)\s*$", g("general", "name") or "")
        version = m.group(1) if m else None
    distro, family, boot = classify(family_txt)
    if not distro:
        return False
    rel, dot = release_split(version)
    f.update(family=family, distro=distro, release=rel, dotrelease=dot, bootstrap=boot,
             arch=norm_arch(g("tree", "arch") or g("general", "arch")))
    f.note(prefix + ".treeinfo", "family=%r version=%r arch=%r" % (family_txt, version, f["arch"]))
    variants = [v.strip() for v in (g("tree", "variants") or g("general", "variants") or "").split(",") if v.strip()]
    repos = {}
    for v in variants:
        path = g("variant-" + v, "repository") or g("variant-" + v, "packages")
        if path:
            repos[v] = posixpath.normpath(posixpath.join(prefix or ".", path.rstrip("/")))
    if repos:
        f["repos"] = repos
        f.note(prefix + ".treeinfo", "variants %s" % ", ".join(sorted(repos)))
    return True


def from_deb_release(r, prefix, suite, f, want_arch):
    data = r.read("%sdists/%s/Release" % (prefix, suite)) or r.read("%sdists/%s/InRelease" % (prefix, suite))
    if not data:
        return False
    kv = kv_file(data)
    distro, family, boot = classify(kv.get("Origin") or kv.get("Label"))
    if not distro:
        return False
    rel, dot = release_split(kv.get("Version"))
    arches = sorted(filter(None, (norm_arch(a) for a in kv.get("Architectures", "").split())))
    f.update(family=family, distro=distro, release=rel, dotrelease=dot, codename=kv.get("Codename") or suite,
             suite=suite, bootstrap=boot, arches_available=arches)
    f.note("%sdists/%s/Release" % (prefix, suite), "Origin=%s Version=%s Codename=%s Architectures=%s" % (
        kv.get("Origin"), kv.get("Version"), kv.get("Codename"), kv.get("Architectures")))
    if want_arch:
        f["arch"] = want_arch if want_arch in arches else None
        if want_arch not in arches:
            f.setdefault("problems", []).append("arch %s not in this archive (%s)" % (want_arch, " ".join(arches)))
    elif len(arches) == 1:
        f["arch"] = arches[0]
    else:
        f.setdefault("ambiguous", []).append("arch: the archive carries %s; pass arch" % " ".join(arches))
    return True


def deb_suites(r, prefix, release=None):
    """
    Release suites of an archive, best first: Debian's 'stable' alias, else the suites whose
    Version is the release asked for ('22.04' or '22'), else the newest Ubuntu LTS, else newest.
    """
    names = [n for n in (r.listdir(prefix + "dists") or []) if re.match(r"^[a-z]+$", n)
             and n not in ("devel", "experimental", "sid", "unstable", "testing", "oldstable", "oldoldstable")]
    if "stable" in names:
        return ["stable"]
    wanted = [int(part) for part in re.findall(r"\d+", release or "")][:2]
    ranked, matching = [], []
    for n in names:
        kv = kv_file(r.read("%sdists/%s/Release" % (prefix, n), 2048))
        m = re.match(r"^(\d+)\.(\d+)", kv.get("Version", ""))
        if m:
            yy, mm = int(m.group(1)), int(m.group(2))
            lts = kv.get("Origin") == "Ubuntu" and mm == 4 and yy % 2 == 0
            ranked.append(((lts, yy, mm), n))
            if wanted and [yy, mm][:len(wanted)] == wanted:
                matching.append(((lts, yy, mm), n))
    return [n for _, n in sorted(matching or ranked, reverse=True)]


def from_os_release(r, f, prefix=""):
    data = r.read(prefix + "etc/os-release") or r.read(prefix + "usr/lib/os-release")
    if not data:
        return False
    kv = kv_file(data)
    distro, family, boot = classify(kv.get("ID"))
    if not distro:
        distro, family, boot = classify(kv.get("ID_LIKE", "").split(" ")[0])
    if not distro:
        return False
    rel, dot = release_split(kv.get("VERSION_ID"))
    if distro == "ubuntu":                                  # VERSION="24.04.3 LTS (Noble Numbat)" holds the point release
        m = re.match(r"^(\d+\.\d+(?:\.\d+)?)", kv.get("VERSION", ""))
        dot = m.group(1) if m else dot
    elif family == "debian" and not dot:                    # Debian's os-release says only "13"; the point is here
        dv = (r.read(prefix + "etc/debian_version") or b"").decode("ascii", "replace").strip()
        if re.match(r"^\d+\.\d+", dv):
            dot = dv
            f.note(prefix + "etc/debian_version", dv)
    f.update(family=family, distro=distro, release=rel, dotrelease=dot,
             codename=kv.get("VERSION_CODENAME") or None, bootstrap="unpack")
    f.note(prefix + "etc/os-release", "ID=%s VERSION_ID=%s VERSION=%r" % (kv.get("ID"), kv.get("VERSION_ID"), kv.get("VERSION")))
    return True


def from_disk_info(r, f, want_arch, suite):
    """Debian/Ubuntu installer ISOs: .disk/info names product, point release and arch; dists/ is on the disc."""
    data = r.read(".disk/info")
    if not data:
        return False
    info = data.decode("utf-8", "replace").strip()
    m = re.match(r'^(.*?)\s+(\d+(?:\.\d+)*)(?:\s+LTS)?\s+"([^"]+)"\s+-\s+.*?\b(amd64|arm64|riscv64|ppc64el|s390x)\b', info)
    if not m:
        return False
    distro, family, boot = classify(m.group(1))
    rel, dot = release_split(m.group(2))
    f.update(family=family, distro=distro, release=rel, dotrelease=dot, arch=norm_arch(m.group(4)), bootstrap=boot)
    f.note(".disk/info", info)
    for s in ([suite] if suite else deb_suites(r, "")):
        g = Finding()
        if from_deb_release(r, "", s, g, f["arch"]):
            f["codename"], f["suite"] = g.get("codename"), s
            f.note("dists/%s/Release" % s, "on-disc archive (pool) present")
            break
    for sq in ("casper/ubuntu-server-minimal.squashfs", "casper/minimal.squashfs", "casper/filesystem.squashfs"):
        if r.listdir(os.path.dirname(sq)) and os.path.basename(sq) in (r.listdir(os.path.dirname(sq)) or []):
            f["rootfs_image"] = sq
            f.note(sq, "a ready root filesystem (squashfs) is on the disc")
            break
    return True


def from_suse_products(r, prefix, f):
    data = r.read(prefix + "media.1/products")
    if not data:
        return False
    line = data.decode("utf-8", "replace").strip().splitlines()[0]
    m = re.match(r"^\S+\s+(\S+?)[ -](\d+(?:\.\d+)?)", line)
    if not m:
        return False
    distro, family, boot = classify(m.group(1))
    rel, dot = release_split(m.group(2))
    f.update(family=family, distro=distro, release=rel, dotrelease=dot, bootstrap=boot)
    f.note(prefix + "media.1/products", line)
    arches = [a for a in ("x86_64", "aarch64", "ppc64le", "s390x", "riscv64") if r.listdir(prefix + a) is not None]
    if arches:
        f["arches_available"] = arches
        if len(arches) == 1:
            f["arch"] = arches[0]
    return True


def from_discinfo(r, f):
    """EL boot/netinstall ISOs: .discinfo = timestamp, version, arch; the family comes from the volume id."""
    data = r.read(".discinfo")
    vol = getattr(r, "volume_id", "")
    if not data or not vol:
        return False
    lines = data.decode("utf-8", "replace").splitlines()
    distro, family, boot = classify(vol)
    if not distro or len(lines) < 3:
        return False
    rel, dot = release_split(lines[1])
    f.update(family=family, distro=distro, release=rel, dotrelease=dot, arch=norm_arch(lines[2]), bootstrap=boot)
    f.note(".discinfo", "version=%s arch=%s (family from volume id %r)" % (lines[1], lines[2], vol))
    if r.listdir("BaseOS") is None and r.listdir("repodata") is None:
        f.setdefault("problems", []).append(
            "no package repository on this disc (a boot/netinstall ISO): use the DVD ISO or a repository URL")
    return True


def from_extra_repo(r, f, want_arch, suite):
    """
    A repository with packages but no identity of its own: a dnf or zypper repository
    that is repodata alone, or an apt archive whose Release names no distribution. It
    cannot seed a build; the rules decide what may be done beside one.
    """
    if r.read("repodata/repomd.xml", 4096):
        f.update(kind="repo-extra", repos={"main": "."})
        f.note("repodata/repomd.xml", "a package repository without a distribution identity")
        return True
    for s in ([suite] if suite else deb_suites(r, "")):
        kv = kv_file(r.read("dists/%s/Release" % s, 65536))
        if not kv.get("Components"):
            continue
        arches = sorted(filter(None, (norm_arch(a) for a in kv.get("Architectures", "").split())))
        f.update(kind="repo-extra", suite=s, arches_available=arches)
        f.note("dists/%s/Release" % s, "an archive without a distribution identity: Origin=%s Label=%s" % (
            kv.get("Origin"), kv.get("Label")))
        if want_arch and arches and want_arch not in arches:
            f.setdefault("problems", []).append("arch %s not in this archive (%s)" % (want_arch, " ".join(arches)))
        elif len(arches) == 1:
            f["arch"] = arches[0]
        return True
    return False


def from_el_root(r, f, want_arch, arches):
    """A Rocky or Alma release root, the directory holding BaseOS and AppStream, for the first arch that answers."""
    for a in arches:
        if from_treeinfo(r, "BaseOS/%s/os/" % a, f):
            f["repos"] = {"BaseOS": "BaseOS/%s/os" % a, "AppStream": "AppStream/%s/os" % a}
            if not want_arch:
                f.setdefault("ambiguous", []).append("arch: an EL release root; took %s, pass arch to choose" % a)
            return True
    return False


def probe_tree(r, f, want_arch, suite, release=None):
    """A repository / directory / ISO: try the self-describing metadata in order of authority."""
    if from_treeinfo(r, "", f):
        if not f.get("repos") and r.listdir("repodata") is not None:
            f["repos"] = {"main": "."}
        return True
    # A release root is the common mirror source and costs a request or two, so it is
    # asked before the apt probing, which costs a dozen: the arch asked for, else what
    # BaseOS lists. A server that lists nothing is swept the long way below.
    listed = None if want_arch else r.listdir("BaseOS")
    arches = [want_arch] if want_arch else [a for a in EL_ARCHES if a in (listed or [])]
    if from_el_root(r, f, want_arch, arches):
        return True
    if from_treeinfo(r, "install/", f):                   # install/: SUSE 16 offline media keep the tree there
        if not f.get("repos") and r.listdir("install/repodata") is not None:
            f["repos"] = {"main": "install"}
        return True
    if from_disk_info(r, f, want_arch, suite):
        return True
    suites = [suite] if suite else deb_suites(r, "", release)
    for s in suites:
        if from_deb_release(r, "", s, f, want_arch):
            if not suite and len(suites) > 1:
                f.setdefault("ambiguous", []).append("suite: took %s (newest LTS) of %s; pass suite" % (s, " ".join(suites)))
            return True
    if suite is None and r.listdir("dists") is None:       # no autoindex: try the well-known stable aliases
        for s in ("stable", "noble", "resolute", "jammy", "trixie", "bookworm"):
            if from_deb_release(r, "", s, f, want_arch):
                f.setdefault("ambiguous", []).append("suite: no dists/ listing; took %s, pass suite to choose" % s)
                return True
    if from_suse_products(r, "", f):
        return True
    if not arches and listed is None and from_el_root(r, f, want_arch, EL_ARCHES):   # no listing: every arch
        return True
    if from_discinfo(r, f):
        return True
    if from_os_release(r, f):                               # an unpacked root filesystem
        f["bootstrap"] = "copy"
        return True
    if from_extra_repo(r, f, want_arch, suite):
        return True
    return False


def arch_in_path(source):
    """The arch a repository path names, the only arch a repository without metadata says."""
    for part in reversed(urllib.parse.urlsplit(source).path.split("/")):
        if norm_arch(part):
            return norm_arch(part)
    return None


def identify(source, want_arch=None, suite=None, deadline=None, release=None):
    """
    One finding for one source, and 0 when it identified cleanly, 2 when it did with
    something to ask the operator about, 1 when it did not.
    """
    want_arch = norm_arch(want_arch) if want_arch else None
    f = Finding(source=source)
    is_url = re.match(r"^(https?|ftp|file)://", source) is not None
    low = source.lower().split("?")[0]
    try:
        if low.endswith(".iso"):
            f["kind"] = "iso"
            iso = Iso(RangeFile(source, deadline) if is_url else LocalFile(source))
            f["volume_id"] = iso.volume_id
            ok = probe_tree(iso, f, want_arch, suite, release)
        elif re.search(r"\.(tar|tar\.gz|tgz|tar\.xz|txz|tar\.bz2)$", low):
            if is_url:
                raise ValueError("a rootfs tarball URL: download it first (detection streams a local file)")
            f["kind"] = "rootfs-tarball"
            t = Tarball(source)
            ok = from_os_release(t, f)
            if ok:
                f["arch"] = elf_arch(t.elf)
                f.note("bin/sh ELF header", "e_machine is %s" % f["arch"])
            if t.oci:
                f["kind"] = "oci-archive"
                f.note("OCI config", "architecture=%s os=%s layers=%d" % (
                    t.oci["architecture"], t.oci["os"], len(t.oci["layers"])))
                f["arch"] = f.get("arch") or norm_arch(t.oci["architecture"])
        elif is_url:
            f["kind"] = "repository"
            h = Http(source, deadline)
            ok = probe_tree(h, f, want_arch, suite, release)
        elif os.path.isdir(source):
            f["kind"] = "directory"
            d = LocalDir(source)
            ok = probe_tree(d, f, want_arch, suite, release)
            if ok and f.get("bootstrap") == "copy":
                f["kind"] = "rootfs-directory"
                for sh in ("usr/bin/bash", "bin/bash", "usr/bin/sh", "bin/sh"):
                    a = elf_arch(d.read(sh, 64))
                    if a:
                        f["arch"] = a
                        f.note(sh + " ELF header", "e_machine is %s" % a)
                        break
        else:
            raise ValueError("not a URL, an .iso, a rootfs tarball or a directory")
    except (OSError, ValueError, tarfile.TarError, struct.error, Expired) as e:
        f["error"] = str(e)
        return f, 1
    if not ok:
        f["error"] = "no self-describing metadata found (.treeinfo, .disk/info, dists/*/Release, media.1/products, os-release)"
        return f, 1
    if f.get("kind") == "repo-extra" and not f.get("arch"):
        f["arch"] = arch_in_path(source)
    if want_arch and not f.get("arch"):
        f["arch"] = want_arch
    if f.get("arch") and want_arch and f["arch"] != want_arch:
        f.setdefault("problems", []).append("source is %s, arch %s was asked" % (f["arch"], want_arch))
    if is_url and f.get("repos") and f.get("kind") in ("repository", "repo-extra"):
        base = source if source.endswith("/") else source + "/"
        f["repo_urls"] = {k: urllib.parse.urljoin(base, v.rstrip("/") + "/") for k, v in f["repos"].items()}
    if f.get("arch"):
        f["arch_family_name"] = DEB_ARCH[f["arch"]] if f.get("family") == "debian" else f["arch"]
    return f, (2 if f.get("ambiguous") or f.get("problems") else 0)


def identify_all(sources: list, want_arch: str = None, suite: str = None,
                 deadline: Deadline = None, release: str = None) -> list:
    """
    One finding per source, read under one deadline for the lot (the caller's where it
    started one earlier), so a slow mirror holds a request for TOTAL_TIMEOUT at most
    whatever the number of sources. The release asked for chooses among an archive's suites.
    """
    deadline = deadline or Deadline()
    return [identify(source, want_arch, suite, deadline=deadline, release=release)[0]
            for source in sources]
