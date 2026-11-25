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
The prometheus plugin: targets, rules, hardware rules and settings.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

# By the canonical name: the loader also imports this directory under a module name of
# its own, and a relative import here would give that copy a second comparison.
from trixd.core.render import triggers
from trixd.plugins.prometheus import peer


def install() -> None:
    """
    Plug this plugin in. The loader calls it once the plugin has loaded; the trigger is
    owned, so unplugging the plugin stops the comparison with it.
    """
    triggers.register("prometheus_hw_rules", peer.current(), owner="prometheus")


__all__ = ["install"]
