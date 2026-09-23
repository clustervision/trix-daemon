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
Writing rendered output without ever leaving a half-written file behind.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import tempfile

import yaml

from trix.common import logging as trixlog
from trix.common.errors import BackendFailure

LOGGER = trixlog.get('files')

BLOCK_START: str = '# --- {manager} managed block, do not edit by hand ---'
BLOCK_END: str = '# --- end {manager} managed block ---'


def write(path: str, content: str, mode: int = 0o644) -> str:
    """
    Write through a temporary file and rename, so a reader never sees a partial one.
    """
    directory = os.path.dirname(path) or '.'
    try:
        os.makedirs(directory, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=directory, prefix='.trix-', suffix='.tmp')
        with os.fdopen(handle, 'w', encoding='utf-8') as target:
            target.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as error:
        raise BackendFailure(f'cannot write {path}: {error}') from None
    LOGGER.debug('wrote %s', path)
    return path


def write_yaml(path: str, document, mode: int = 0o644) -> str:
    """
    The same, for a document.
    """
    return write(path, yaml.safe_dump(document, default_flow_style=False, sort_keys=False,
                                      explicit_start=True), mode)


def read_text(path: str, fallback: str = '') -> str:
    """
    The file, or the fallback when it is not there.
    """
    if not os.path.isfile(path):
        return fallback
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()


def set_managed_block(path: str, manager: str, content: str, mode: int = 0o644) -> str:
    """
    Replace our block in a file somebody else also writes to.

    Everything outside the two markers is left exactly as it was found, which is
    what lets a site keep its own lines in the same file.
    """
    start = BLOCK_START.format(manager=manager)
    end = BLOCK_END.format(manager=manager)
    existing = read_text(path)
    block = f'{start}\n{content.rstrip()}\n{end}\n'

    if start in existing and end in existing:
        head = existing.split(start)[0]
        tail = existing.split(end, 1)[1].lstrip('\n')
        updated = f'{head}{block}{tail}'
    else:
        separator = '' if not existing or existing.endswith('\n') else '\n'
        updated = f'{existing}{separator}{block}'
    return write(path, updated, mode)


def get_managed_block(path: str, manager: str) -> str:
    """
    What our block currently holds, or an empty string.
    """
    start = BLOCK_START.format(manager=manager)
    end = BLOCK_END.format(manager=manager)
    existing = read_text(path)
    if start not in existing or end not in existing:
        return ''
    return existing.split(start, 1)[1].split(end, 1)[0].strip('\n')
