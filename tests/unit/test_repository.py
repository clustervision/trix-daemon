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
The repository and the package built from it: house rules for every tracked file,
and a wheel that carries everything the daemon reads at run time.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import ast
import glob
import json
import os
import re
import subprocess
import sys
import zipfile

import pytest

from trixd.common.constants import DAEMON_RELEASE

# What the daemon's own environment variables start with, which is what makes dropping them
# enough to build against a clean environment. Asserted below rather than assumed.
DAEMON_ENV_PREFIX = "TRIXD_"

# How a call going out over TLS is spelled, on the requests module or on a session of it.
HTTP_CALLS = ("get", "post", "put", "patch", "delete", "head", "options", "request")
# The one helper that says what a certificate is checked against.
TRUST_HELPER = "system_trust"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEADER = "This code is part of the TrinityX software suite"
# Files that are their own kind of text: the licence itself, plain READMEs, git's own.
# Files that are their own kind of text, and the JSON tools/build_schemas.py writes,
# which carries its licence in its own fields since JSON has no comments. The golden
# files are whole slurm and genders files as another writer leaves them, so a header of
# ours in one would be bytes under test.
NO_HEADER = re.compile(r"(^|/)(LICENSE\.txt|README|\.gitattributes)$|^docs/.*\.json$"
                       r"|^tests/unit/golden/")
# Shipped as their authors made them: Swagger UI, unchanged, and images, which are bytes.
AS_IS = re.compile(r"^trixd/static/vendor/|\.(png|ico)$")


def _tracked() -> list:
    """
    Every file git tracks here, or a skip where there is no git, as in an sdist.
    """
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                text=True, check=True).stdout.split("\n")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [name for name in listed if name]


def _read(name: str) -> bytes:
    """
    One tracked file, as bytes.
    """
    with open(os.path.join(ROOT, name), "rb") as handle:
        return handle.read()


def test_every_tracked_file_is_plain_ascii():
    """
    No typographic dashes, quotes, ellipses or arrows anywhere: they read differently
    in every terminal and editor, and the house style is plain text.
    """
    offending = []
    for name in [item for item in _tracked() if not AS_IS.search(item)]:
        for number, line in enumerate(_read(name).decode("utf-8").splitlines(), 1):
            if any(ord(char) > 127 for char in line):
                offending.append(f"{name}:{number}")
    assert not offending, offending


def test_every_tracked_file_carries_the_licence_header():
    """
    The GPL notice at the top of every file but the licence and the plain READMEs.
    """
    missing = [name for name in _tracked() if not NO_HEADER.search(name) and not AS_IS.search(name)
               and HEADER not in _read(name).decode("utf-8")[:2000]]
    assert not missing, missing


def test_the_version_is_the_release_line_and_the_commit_time():
    """
    The same commit gives the same version on every run, so pip sees one version.
    """
    environment = dict(os.environ, CI_COMMIT_TIMESTAMP="2026-10-07T08:00:00Z")
    versions = set()
    for _ in range(2):
        answer = subprocess.run([sys.executable, "setup.py", "--version"], cwd=ROOT,
                                capture_output=True, text=True, env=environment, check=True)
        versions.add(answer.stdout.strip().splitlines()[-1])
    assert versions == {f"{DAEMON_RELEASE}.1791360000"}


def test_the_wheel_carries_every_file_the_daemon_ships(tmp_path):
    """
    Every tracked file under trixd/ is in the wheel, under the name the pipeline expects,
    with the dependencies requirements.txt pins.
    """
    pytest.importorskip("setuptools")
    tracked = [name for name in _tracked() if name.startswith("trixd/")]
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                            "--no-build-isolation", "-q", "-w", str(tmp_path), ROOT],
                           capture_output=True, text=True)
    if built.returncode != 0:
        pytest.skip(f"no wheel could be built here: {built.stderr[-300:]}")

    wheels = glob.glob(str(tmp_path / "*.whl"))
    assert len(wheels) == 1, wheels
    assert re.fullmatch(rf"trixd-{re.escape(DAEMON_RELEASE)}\.\d+-py3-none-any\.whl",
                        os.path.basename(wheels[0])), wheels[0]
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        metadata = wheel.read(next(name for name in names if name.endswith(".dist-info/METADATA")))
    assert not [name for name in tracked if name not in names], "left out of the wheel"

    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as handle:
        pins = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    required = re.findall(r"^Requires-Dist: (.+)$", metadata.decode("utf-8"), re.M)
    normal = {re.sub(r"[\s()]", "", item) for item in required}
    assert normal == set(pins), (normal, pins)


def _shape(document: dict) -> dict:
    """
    What the API promises, without the formatting a library version may change: each path
    and verb, its summary and description, who may call it, whether it takes a body, and
    its parameters.

    The description is in here because it is the published text a reader believes, and it
    is assembled from the manifest by this repository rather than formatted by the library
    - so comparing it catches a document that still describes what an endpoint used to do,
    which comparing structure alone cannot. That had happened: the committed document was
    still saying a node signs in with a username and password after the terms had changed.
    """
    shape = {}
    for path, operations in document["paths"].items():
        for verb, operation in operations.items():
            shape[f"{verb.upper()} {path}"] = {
                "summary": operation.get("summary", ""),
                "description": operation.get("description", ""),
                "access": operation.get("x-trixd-access", ""),
                "minimum": operation.get("x-trixd-minimum", ""),
                "body": "requestBody" in operation,
                "parameters": sorted(item["name"] for item in operation.get("parameters", [])),
            }
    return shape


# FastAPI's own models, named here because what is in them is FastAPI's wording and
# FastAPI's fields rather than anything written down in this repository.
LIBRARY_SCHEMAS = ("HTTPValidationError", "ValidationError")


def _examples(holder: dict) -> list:
    """
    The example values declared on a media type, a parameter or a field, as sorted JSON
    text. The library carries them three ways - one bare example, a list of them, or a
    dict keyed by a name it invents - so the wrapper is taken off and the values, which
    are the manifest's, are what gets compared.
    """
    declared = []
    if "example" in holder:
        declared.append(holder["example"])
    examples = holder.get("examples")
    if isinstance(examples, dict):
        declared += [item.get("value") if isinstance(item, dict) else item
                     for item in examples.values()]
    elif isinstance(examples, list):
        declared += examples
    return sorted(json.dumps(item, sort_keys=True) for item in declared)


def _content(holder: dict) -> dict:
    """
    What a body or an answer carries: each media type, the model it names, and the
    examples declared for it. The model is named rather than read into - the name is a
    class in trixd/, while the schema under it is the library's rendering of that class.
    """
    carried = {}
    for media, body in holder.get("content", {}).items():
        named = body.get("schema", {}).get("$ref", "")
        carried[media] = {"model": named.rsplit("/", 1)[-1], "examples": _examples(body)}
    return carried


def _library_answer(answer: dict) -> bool:
    """
    Whether an answer is one FastAPI added rather than one a manifest declared. It adds
    its own validation answer, in its own words, to every route that takes an argument.
    A manifest may declare a 422 of its own and one does; that one names our own Error
    model, so what this skips is the library's answer and not the code's.
    """
    models = {body.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
              for body in answer.get("content", {}).values()}
    return bool(models) and models <= set(LIBRARY_SCHEMAS)


def _promises(document: dict) -> dict:
    """
    Everything the document promises a reader that is authored in this repository, and
    nothing the library decides how to render.

    Ours, so compared: every description and example, down to the field; the field names
    and which of them are required; the constraints a manifest declares; the response
    codes and their wording; the model each body and answer names; the tags, the security
    schemes and the info block. Every one of those is written in a plugin manifest, a
    route declaration or a model in trixd/, so a difference between the committed
    document and a fresh one is a real divergence - the published document promising a
    reader something the code no longer does.

    The library's, so left out: the spec version, the generated operationId, a field's
    auto-titled title, a declared default, and the JSON Schema a type turns into (anyOf
    against null, additionalProperties, items, format). requirements.txt pins fastapi
    exactly but takes pydantic as a floor, so the installed pydantic really does differ
    between machines and really does re-render those - comparing them would fail in CI
    with nothing here changed, which is what _shape was right to leave out. The gap it
    left is that it dropped the authored content along with the rendering.

    So do not widen this to the whole document. Add to it only what is written down in
    this repository, and leave to _shape's reasoning anything the library formats.
    """
    components = document.get("components", {})
    promises = {"info": document["info"],
                "securitySchemes": components.get("securitySchemes", {})}
    for name, schema in components.get("schemas", {}).items():
        if name in LIBRARY_SCHEMAS:
            continue
        promises[f"schema {name}"] = {
            "description": schema.get("description", ""),
            "required": sorted(schema.get("required", [])),
            "fields": {field: {"description": rule.get("description", ""),
                               "pattern": rule.get("pattern", ""),
                               "examples": _examples(rule)}
                       for field, rule in schema.get("properties", {}).items()},
        }
    for path, operations in document["paths"].items():
        for verb, operation in operations.items():
            body = operation.get("requestBody") or {}
            promises[f"{verb.upper()} {path}"] = {
                "tags": sorted(operation.get("tags", [])),
                "security": sorted(scheme for item in operation.get("security", [])
                                   for scheme in item),
                "parameters": {item["name"]: {"in": item.get("in", ""),
                                              "required": item.get("required", False),
                                              "description": item.get("description", ""),
                                              "examples": _examples(item)}
                               for item in operation.get("parameters", [])},
                "body": {"required": body.get("required", False),
                         "content": _content(body)} if body else {},
                "answers": {code: {"description": answer.get("description", ""),
                                   "content": _content(answer)}
                            for code, answer in operation.get("responses", {}).items()
                            if not _library_answer(answer)},
            }
    return promises


def test_the_published_schemas_match_the_code(tmp_path):
    """
    docs/openapi.json and docs/manifest.schema.json are what the code builds: the routes
    they publish, and the text and examples they promise a reader. When this fails, run
    python tools/build_schemas.py and commit what it writes.
    """
    # Built against an environment with the daemon's own variables taken out. The build is
    # a subprocess, so it inherits whatever an earlier test left behind - and the Luna ini
    # one is set directly rather than through monkeypatch, so it outlives the test that set
    # it. Inherited, this compares the document this suite's leftovers produce rather than
    # the one the code does, and it does so only when the whole suite runs.
    clean = {name: value for name, value in os.environ.items()
             if not name.startswith(DAEMON_ENV_PREFIX)}
    built = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_schemas.py"),
                            str(tmp_path)], capture_output=True, text=True, cwd=ROOT,
                           env=clean)
    assert built.returncode == 0, built.stderr[-500:]
    for name in ("openapi.json", "manifest.schema.json"):
        with open(os.path.join(ROOT, "docs", name), encoding="utf-8") as handle:
            committed = json.load(handle)
        with open(tmp_path / name, encoding="utf-8") as handle:
            fresh = json.load(handle)
        if name == "openapi.json":
            assert _shape(committed) == _shape(fresh), "run python tools/build_schemas.py"
            held, builds = _promises(committed), _promises(fresh)
            assert sorted(held) == sorted(builds), "run python tools/build_schemas.py"
            apart = {key: {"docs/openapi.json": held[key], "the code": builds[key]}
                     for key in sorted(held) if held[key] != builds[key]}
            assert not apart, (f"docs/openapi.json promises what the code no longer "
                               f"builds, at {sorted(apart)}: {apart}; "
                               f"run python tools/build_schemas.py")
        else:
            assert committed == fresh, "run python tools/build_schemas.py"


def test_every_published_route_says_who_may_call_it():
    """
    Every operation in docs/openapi.json carries its access, and a role route its minimum.
    """
    import json

    with open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8") as handle:
        shape = _shape(json.load(handle))
    assert shape, "an empty document"
    for operation, facts in shape.items():
        assert facts["access"] in ("public", "role", "node", "forward"), operation
        if facts["access"] == "role":
            assert facts["minimum"] in ("none", "reader", "operator", "manager", "rootus"), operation
    public = sorted(op for op, facts in shape.items() if facts["access"] == "public")
    assert public == ["GET /api/v1/prometheus/renders/targets", "GET /api/v1/system/health",
                      "GET /api/v1/system/version", "POST /api/v1/auth/node/{node}",
                      "POST /api/v1/auth/token"], public


def test_the_daemon_itself_knows_nothing_about_luna():
    """
    Luna is a plugin, not a part of the daemon. Nothing the daemon is made of may import
    it: the moment one module does, a controller without the plugin stops being able to
    start, and the next Luna-shaped thing gets added to the core instead of beside it.
    This walks the imports rather than checking the one module that prompted it, so the
    next import is caught too.
    """
    import ast

    offenders = []
    for name in _tracked():
        if not name.endswith(".py"):
            continue
        if not (name.startswith("trixd/core/") or name.startswith("trixd/api/")
                or name.startswith("trixd/common/") or name in ("trixd/app.py",
                                                                "trixd/server.py")):
            continue
        for node in ast.walk(ast.parse(_read(name).decode("ascii"))):
            if isinstance(node, ast.Import):
                found = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                found = [node.module or ""]
            else:
                continue
            for module in found:
                if module.startswith("trixd.plugins") or module.startswith("trixd.core.luna"):
                    offenders.append(f"{name}:{node.lineno} imports {module}")
    assert offenders == [], "the daemon reaches into a plugin:\n" + "\n".join(offenders)


def test_the_luna_plugin_is_reachable_as_one_module():
    """
    The loader imports a plugin directory under its own module name, so a plugin that
    imported itself relatively would hold a second client with a second token cache.
    Every import of the luna plugin's own modules is therefore absolute.
    """
    import ast

    offenders = []
    for name in _tracked():
        if not name.startswith("trixd/plugins/luna/") or not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(_read(name).decode("ascii"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                offenders.append(f"{name}:{node.lineno} imports relatively")
    assert offenders == [], "\n".join(offenders)


def test_every_environment_variable_the_daemon_reads_carries_the_prefix():
    """
    What makes dropping TRIXD_* enough to build against a clean environment. A variable read
    under some other name would survive that, steer the build, and say nothing - which is
    the order-dependent failure the clean environment exists to stop, in a narrower form.

    Read from the shipped tree rather than listed, so a variable added later has to carry
    the prefix or change this. Both shapes are caught: a name passed straight to the
    environment, and a module constant named <SOMETHING>_ENV holding one.
    """
    import trixd

    root = os.path.dirname(trixd.__file__)
    reads = {}
    for folder, _, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            source = os.path.join(folder, name)
            with open(source, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=source)
            where = os.path.relpath(source, os.path.dirname(root))
            for node in ast.walk(tree):
                asked = None
                if isinstance(node, ast.Call) and node.args:
                    called = node.func
                    reading = (isinstance(called, ast.Attribute)
                               and called.attr in ("get", "getenv"))
                    if reading and isinstance(node.args[0], ast.Constant):
                        asked = node.args[0].value
                        # environ.get and getenv, not every .get in the tree.
                        if not _reads_the_environment(called):
                            asked = None
                elif isinstance(node, ast.Subscript) and _reads_the_environment(node.value):
                    if isinstance(node.slice, ast.Constant):
                        asked = node.slice.value
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    asked = _named_env_constant(node)
                if isinstance(asked, str) and asked:
                    reads.setdefault(asked, f"{where}:{node.lineno}")

    assert reads, "no environment variable was found at all, so this proves nothing"
    stray = {name: where for name, where in reads.items()
             if not name.startswith(DAEMON_ENV_PREFIX)}
    assert not stray, (
        f"these are read without the {DAEMON_ENV_PREFIX} prefix, so taking that prefix out "
        f"does not give a clean environment: {stray}")


def _reads_the_environment(node) -> bool:
    """
    Whether this expression is os.environ or os itself, as os.getenv is reached through.
    """
    if isinstance(node, ast.Attribute):
        return node.attr in ("environ", "getenv") or _reads_the_environment(node.value)
    return isinstance(node, ast.Name) and node.id in ("os", "environ")


def _named_env_constant(node):
    """
    The string a constant named <SOMETHING>_ENV holds, which is how two of them are reached.
    """
    targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
    names = [one.id for one in targets if isinstance(one, ast.Name)]
    if not any(name.endswith("_ENV") for name in names):
        return None
    return node.value.value if isinstance(node.value, ast.Constant) else None


def test_every_setting_a_plugin_defaults_is_declared_by_the_schema_that_takes_it():
    """
    A key in a plugin's defaults that its manifest schema does not declare cannot be set
    over the API at all: the request model the manifest builds ignores what it does not
    declare, so the key never reaches the handler, the merge finds nothing to change, and
    the call answers 200 with the old value still in it. Read from the plugin tree and from
    each manifest's own schema, so the next setting added to a defaults dict is held to this
    without anything here being told about it.
    """
    import ast
    import yaml

    checked, missing = [], []
    for manifest in sorted(glob.glob(os.path.join(ROOT, "trixd", "plugins", "*",
                                                  "manifest.yaml"))):
        with open(manifest, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        schemas = document.get("schemas") or {}
        backend = os.path.join(os.path.dirname(manifest),
                               f"{document.get('backend', 'backend')}.py")
        tree = ast.parse(_read(os.path.relpath(backend, ROOT)).decode("ascii"))
        merged = _merged_defaults(tree)
        for endpoint in document.get("endpoints") or []:
            keys = merged.get(endpoint.get("handler"))
            if not keys or not endpoint.get("schema"):
                continue
            where = f"{document['plugin']} {endpoint['schema']}"
            checked.append(where)
            missing += [f"{where} does not declare {key}, which {endpoint['handler']}() "
                        f"merges as a default"
                        for key in sorted(keys - set(schemas.get(endpoint["schema"]) or {}))]

    assert checked, "no plugin was found to merge any defaults, so this proves nothing"
    assert missing == [], ("a setting the code defaults is dropped before its handler sees "
                           "it:\n" + "\n".join(missing))


def _merged_defaults(tree) -> dict:
    """
    Per backend method, the keys of the defaults it merges a request body into: the last
    argument of declarations.merge, where it names a dict the module holds or is one.
    """
    held = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        keys = {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
        for one in targets:
            if isinstance(one, ast.Name):
                held[one.id] = keys

    found = {}
    for owner in ast.walk(tree):
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(owner):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            reached = node.func.value
            if node.func.attr != "merge" or not (isinstance(reached, ast.Name)
                                                 and reached.id == "declarations"):
                continue
            defaults = node.args[4] if len(node.args) > 4 else next(
                (word.value for word in node.keywords if word.arg == "defaults"), None)
            if isinstance(defaults, ast.Name):
                found.setdefault(owner.name, set()).update(held.get(defaults.id) or set())
            elif isinstance(defaults, ast.Dict):
                found.setdefault(owner.name, set()).update(
                    key.value for key in defaults.keys if isinstance(key, ast.Constant))
    return found


def test_no_certificate_is_ever_checked_against_the_library_default():
    """
    Every call the daemon makes over TLS says what the certificate is checked against, and
    what it names traces back to the one helper. The bundle `requests` reaches for when
    nobody says - `verify=True`, or no `verify` at all - is certifi's inside the daemon's
    virtual environment, and certifi has never heard of this cluster, so a check against
    it cannot succeed and a site that turns checking on gets a daemon that talks to
    nothing.

    Found by walking the shipped tree, not by listing the files that do it today: the bug
    this closes is a second place quietly keeping its own answer, and a test naming the
    places has to be remembered by whoever adds the third.
    """
    sources = _shipped_sources()
    producers = _trust_producers(sources)

    calls, offending = {}, []
    for name, tree in sources:
        filled = _filled_from_a_producer(tree, producers)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in HTTP_CALLS or not _over_http(node.func.value):
                continue
            where = f"{name}:{node.lineno}"
            calls[where] = name
            said = [word.value for word in node.keywords if word.arg == "verify"]
            if not said:
                offending.append(f"{where} says nothing about the certificate")
            elif not _from_a_producer(said[0], producers, filled):
                offending.append(f"{where} does not check against {TRUST_HELPER}()")

    sending = {name for name, tree in sources if _sends_over_http(tree)}
    assert sending, "nothing reaches for requests to send with, so this proves nothing"
    missed = sorted(sending - set(calls.values()))
    assert not missed, (
        "these reach for requests to send with and no call of theirs was found, so the "
        f"walk went past one: {missed}")
    assert not offending, "\n".join(offending)
    assert any(isinstance(node, ast.FunctionDef) and node.name == TRUST_HELPER
               for _, tree in sources for node in ast.walk(tree)), (
                   f"nothing defines {TRUST_HELPER}() any more, so what the sites above "
                   "were matched against is gone and their silence proves nothing")


def _sends_over_http(tree) -> bool:
    """
    Whether a module reaches for `requests` to send something, rather than only to name
    what it raises and catches.

    A module that names it nowhere but in a raise or an except clause is using the
    library's error vocabulary: it has no call for the walk to find, and demanding one of
    it anyway ends as a list of the files allowed not to have one, which is the parallel
    list this test exists to do without. Every name the library is reached by counts, the
    module and whatever is imported out of it alike, so a sender cannot become exempt by
    importing the thing it sends with.
    """
    handles = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            handles.update(one.asname or one.name.split(".")[0] for one in node.names
                           if one.name == "requests" or one.name.startswith("requests."))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "requests":
            handles.update(one.asname or one.name for one in node.names)
    naming_errors = set()
    for node in ast.walk(tree):
        named = (node.exc if isinstance(node, ast.Raise)
                 else node.type if isinstance(node, ast.ExceptHandler) else None)
        if named is not None:
            naming_errors.update(id(one) for one in ast.walk(named))
    return any(isinstance(node, ast.Name) and node.id in handles
               and id(node) not in naming_errors for node in ast.walk(tree))


def _shipped_sources() -> list:
    """
    Every tracked module under trixd/, parsed, as (name, tree).
    """
    return [(name, ast.parse(_read(name).decode("ascii"), filename=name))
            for name in _tracked()
            if name.startswith("trixd/") and name.endswith(".py")]


def _from_a_producer(node, producers: set, filled: set) -> bool:
    """
    Whether an expression can only hold what the helper answered: it is False, it calls or
    reads a producer, or it is a name this module filled from one. The expression itself
    is matched rather than searched, so a call that merely carries a producer along - a
    request sent with one - is not mistaken for one.

    Either arm of a choice is enough, because the other is the file a site named for
    itself; `True` matches nothing, which is the whole point.
    """
    if isinstance(node, ast.Constant):
        return node.value is False
    if isinstance(node, ast.IfExp):
        return (_from_a_producer(node.body, producers, filled)
                or _from_a_producer(node.orelse, producers, filled))
    if isinstance(node, ast.BoolOp):
        return any(_from_a_producer(one, producers, filled) for one in node.values)
    if isinstance(node, ast.Call):
        called = node.func
        return (called.attr if isinstance(called, ast.Attribute)
                else getattr(called, "id", "")) in producers
    if isinstance(node, ast.Attribute):
        return node.attr in producers
    if isinstance(node, ast.Name):
        return node.id in filled
    return False


def _trust_producers(sources: list) -> set:
    """
    Every function in the tree whose answer can only be what the helper gave it, grown
    from the helper itself until the set stops growing. Grown rather than listed so the
    length of the chain between a call and the helper does not matter, and so a chain
    someone lengthens later needs nothing changed here.
    """
    found = {TRUST_HELPER}
    growing = True
    while growing:
        growing = False
        for _, tree in sources:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in found:
                    continue
                answers = [one.value for one in ast.walk(node)
                           if isinstance(one, ast.Return) and one.value is not None]
                if answers and all(_from_a_producer(one, found, set()) for one in answers):
                    found.add(node.name)
                    growing = True
    return found


def _filled_from_a_producer(tree, producers: set) -> set:
    """
    The names one module fills from a producer, grown the same way. Flat over the module
    rather than per function, because a module here gives one name one meaning.
    """
    filled = set()
    growing = True
    while growing:
        growing = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not _from_a_producer(node.value, producers, filled):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for one in targets:
                if isinstance(one, ast.Name) and one.id not in filled:
                    filled.add(one.id)
                    growing = True
    return filled


def _over_http(node) -> bool:
    """
    Whether this is the requests module, or a session held on one.
    """
    if isinstance(node, ast.Attribute):
        return node.attr in ("requests", "session") or _over_http(node.value)
    return isinstance(node, ast.Name) and node.id in ("requests", "session")
