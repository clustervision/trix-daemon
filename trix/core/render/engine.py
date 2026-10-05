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
Render on change, answer every poll from the render, reconcile on start.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import threading
import time

from sqlalchemy import select

from trix.common import logging as trixlog
from trix.common.errors import NotFound
from trix.common.settings import seconds
from trix.db import engine as database
from trix.db import models

LOGGER = trixlog.get("render")


class Render:
    """
    One declared output and the backend method that produces it.
    """

    def __init__(self, plugin: str, path: str, spec, producer) -> None:
        """
        One render, its declaration and what produces it.
        """
        self.plugin = plugin
        self.path = path
        self.spec = spec
        self.producer = producer
        self.key = f"{plugin}/{path}"
        self.coalesce = seconds(spec.coalesce, 2)
        self.document = None
        self.rendered_at: float = 0.0
        self.last_error: str = ""

    def wants(self, changed: str) -> bool:
        """
        Whether a change to this kind of object is one of our inputs.
        """
        if not changed:
            return True
        return any(str(item) == changed or str(item).startswith(f"{changed}.")
                   or changed.startswith(f"{item}.") for item in self.spec.inputs)


class Engine:
    """
    Every render this process knows about.
    """

    def __init__(self) -> None:
        """
        One render, its declaration and what produces it.
        """
        self.renders: dict = {}
        self.lock = threading.RLock()
        self.pending: dict = {}
        self.trailing: dict = {}

    def forget(self, plugin: str) -> None:
        """
        Drop every render a plugin declared, before it is reloaded.
        """
        with self.lock:
            for key in [item for item in self.renders if item.startswith(f"{plugin}/")]:
                self.renders.pop(key, None)
                timer = self.trailing.pop(key, None)
                if timer is not None:
                    timer.cancel()

    def register(self, plugin: str, path: str, spec, producer) -> Render:
        """
        Take a render and the backend method that produces it.
        """
        with self.lock:
            item = Render(plugin, path, spec, producer)
            self.renders[item.key] = item
            LOGGER.debug("render %s declared, inputs %s", item.key, spec.inputs)
            return item

    def reader(self, plugin: str, path: str):
        """
        The path operation that answers a poll from what was rendered.
        """

        def read(caller=None):
            """
            Serve what was rendered, rather than rendering again.
            """
            item = self.renders.get(f"{plugin}/{path}")
            if item is None:
                raise NotFound(f"no render named {plugin}/{path}")
            if item.document is None:
                self.run(item)
            if item.document is None:
                raise NotFound(f"{plugin}/{path} has not rendered yet: {item.last_error}")
            return item.document

        return read

    def run(self, item: Render) -> bool:
        """
        Produce the output. Never leaves a half-written result behind.
        """
        with self.lock:
            try:
                produced = item.producer()
            except Exception as error:
                item.last_error = str(error)
                LOGGER.error("render %s failed, keeping the previous output: %s",
                             item.key, error)
                self._record(item)
                return False
            item.document = produced
            item.rendered_at = time.time()
            item.last_error = ""
            LOGGER.info("rendered %s", item.key)
            self._record(item)
            return True

    def run_all(self) -> int:
        """
        The reconcile on start: every render, once, over what is on disk.
        """
        done = 0
        for item in list(self.renders.values()):
            if self.run(item):
                done += 1
        return done

    def notify(self, changed: str = "") -> list:
        """
        A change landed. Render what declared it as an input, now or when its window closes.
        """
        woken = []
        now = time.monotonic()
        for item in list(self.renders.values()):
            if not item.wants(changed):
                continue
            last = self.pending.get(item.key, 0.0)
            if now - last < item.coalesce:
                # Inside the window: once more when it closes, so a burst's last change is drawn.
                self._trail(item.key, item.coalesce - (now - last))
                continue
            self.pending[item.key] = now
            woken.append(item.key)
            self.run(item)
        return woken

    def _trail(self, key: str, delay: float) -> None:
        """
        One deferred render per render, however many changes land inside the window.
        """
        with self.lock:
            if key in self.trailing:
                return
            timer = threading.Timer(delay, self._fire, args=(key,))
            timer.daemon = True
            self.trailing[key] = timer
            timer.start()

    def _fire(self, key: str) -> None:
        """
        The window has closed: render what the burst left behind.
        """
        with self.lock:
            self.trailing.pop(key, None)
            item = self.renders.get(key)
            if item is None:
                return
            self.pending[key] = time.monotonic()
        self.run(item)

    def listing(self) -> list:
        """
        Every render and when it last ran.
        """
        return [{"render": item.key,
                 "inputs": item.spec.inputs,
                 "rendered_at": item.rendered_at,
                 "error": item.last_error} for item in sorted(
                     self.renders.values(), key=lambda entry: entry.key)]

    def _record(self, item: Render) -> None:
        """
        Keep the local note of when this last ran. Never fatal.
        """
        try:
            handle = database.session()
        except Exception:
            return
        try:
            row = handle.scalar(select(models.RenderState)
                                .where(models.RenderState.render == item.key))
            if row is None:
                row = models.RenderState(render=item.key)
                handle.add(row)
            row.rendered_at = models.now()
            row.last_error = item.last_error
            handle.commit()
        except Exception as error:
            handle.rollback()
            LOGGER.warning("could not record render state for %s: %s", item.key, error)
        finally:
            handle.close()


_engine: Engine = Engine()


def current() -> Engine:
    """
    The render engine for this process.
    """
    return _engine
