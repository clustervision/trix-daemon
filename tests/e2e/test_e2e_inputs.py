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
What a careless or hostile caller sends: odd path values, bodies of the wrong shape and
queries out of range, as rootus so nothing stops at the gate. The answer may be any
refusal; it is never a 500, and the daemon stays up.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from urllib.parse import quote

import pytest

from trixd.core.plugins.manifest import BODY_VERBS

from tests.e2e.stack import MARK, operations

# Every value carries the mark, so anything a route creates from it is cleaned up.
PATH_VALUES = ("x", "..", "a b", "x" * 300, "unicode-" + chr(0xfc) + chr(0xf1) + chr(0xef), "%2e%2e", "a;id",
               "-rf", "0", "' OR '1'='1", "x?y=1", "x#frag", "$(id)", "x\\y")
BODIES = ("[]", '"text"', "7", "null", '{"e2e-unknown": 1}', '{"a": {"b": {"c": {"d": 1}}}}',
          '"' + "x" * 100000 + '"', "{not json")


def _path(path: str, value: str) -> str:
    """
    The template with every variable set to a marked, encoded value.
    """
    encoded = quote(f"{MARK}{value}", safe="")
    return "/".join(encoded if part.startswith("{") else part for part in path.split("/"))


@pytest.mark.parametrize("value", PATH_VALUES, ids=[f"v{n}" for n in range(len(PATH_VALUES))])
def test_an_odd_path_value_is_never_a_500(stack, operation, value):
    """
    Spaces, dots, quotes, shell characters and long names in a path variable.
    """
    verb, path, _ = operation
    if "{" not in path:
        pytest.skip("no path variable")
    body = {} if verb in BODY_VERBS else None
    mark = len(stack.dropped)
    answer = stack.call(verb, _path(path, value), stack.rootus(), json=body)
    # Before the status: a worker killed by this value raises the same ConnectionError as a
    # connection the server had already closed, and the retry answers a tidy refusal from a
    # worker that is well. A dead worker is worse than the 500 this looks for.
    stack.unbroken(mark)
    assert answer.status_code != 500, answer.text


test_an_odd_path_value_is_never_a_500.access = ("role", "forward")


@pytest.mark.parametrize("raw", BODIES, ids=[f"b{n}" for n in range(len(BODIES))])
def test_a_body_of_the_wrong_shape_is_refused_not_a_500(stack, operation, raw):
    """
    A list, a string, a number, null, unknown or deep fields, a huge string, broken JSON.
    """
    verb, path, described = operation
    if verb not in BODY_VERBS:
        pytest.skip("takes no body")
    mark = len(stack.dropped)
    answer = stack.call(verb, _path(path, "x"), stack.rootus(), data=raw,
                        headers={"Content-Type": "application/json"})
    stack.unbroken(mark)
    assert answer.status_code != 500, answer.text
    if "requestBody" in described and raw in ("[]", '"text"', "7", "{not json"):
        assert answer.status_code in (400, 404, 422), (raw, answer.status_code, answer.text)


test_a_body_of_the_wrong_shape_is_refused_not_a_500.access = ("role", "forward")


@pytest.mark.parametrize("query", [{"limit": "0"}, {"limit": "-1"}, {"limit": "1000000000"},
                                   {"limit": "x"}, {"since": "yesterday"},
                                   {"outcome": "maybe"}, {"principal": "x" * 5000}])
def test_the_audit_trail_refuses_a_query_out_of_range(stack, query):
    """
    Each out-of-range filter is a 400 or a 422, and a long principal is simply no match.
    """
    answer = stack.call("GET", "/system/audit", stack.rootus(), params=query)
    if "principal" in query:
        assert answer.status_code == 200 and answer.json()["audit"] == []
    else:
        assert answer.status_code in (400, 422), (query, answer.text)


def test_every_verb_that_publishes_a_body_is_one_the_fuzz_sends_a_body_for(document):
    """
    BODY_VERBS is the daemon's own list and it decides whether the fuzz above sends a body
    at all; a verb missing from it is skipped as taking none. So the list and the operations
    that declare a request body are a parallel pair, and the way that pair goes wrong is the
    usual one: the product learns a new body verb and the list does not, the items that
    should have fuzzed it skip, and the skip is one of hundreds nobody reads.

    Enumerated from the published document rather than listed here, so the next verb is
    covered without anyone remembering to add it.
    """
    carrying = {verb for verb, _, described in operations(document) if "requestBody" in described}
    missing = sorted(carrying - set(BODY_VERBS))
    assert not missing, (
        f"these verbs publish a request body and BODY_VERBS does not hold them, so the "
        f"body fuzz skips every operation that uses one: {missing}")


def test_the_daemon_is_still_healthy_after_all_of_it(stack):
    """
    Last in this file: nothing above took it down.

    Health alone cannot say so where the daemon is supervised - a worker that died is
    replaced and the one that answers this is well - so what every item above recorded of
    its own request is read here too.
    """
    assert stack.call("GET", "/system/health").status_code == 200
    assert not stack.dropped, (
        f"the daemon dropped the connection during the run: {stack.dropped}. call() retries "
        f"one, so the items that saw this passed on the retry's answer")
