"""Run blocking core calls (disk, Docker, HTTP) off the GUI thread."""

from __future__ import annotations

import contextlib
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from fmapp.core.remote import RemoteError

_live: set[_Relay] = set()


class _Relay(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], relay: _Relay) -> None:
        super().__init__()
        self.fn, self.relay = fn, relay

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # surfaced to the UI, never swallowed
            if not isinstance(exc, RemoteError):  # an unreachable server isn't a bug
                traceback.print_exc()
            self._emit(self.relay.failed, str(exc) or exc.__class__.__name__)
        else:
            self._emit(self.relay.done, result)

    @staticmethod
    def _emit(signal, value: Any) -> None:
        # During shutdown the relay may already be gone.
        with contextlib.suppress(RuntimeError):
            signal.emit(value)


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    # The relay lives on the GUI thread, so its signals are delivered there (queued).
    relay = _Relay()
    _live.add(relay)

    def finish(callback: Callable[[Any], None] | None, value: Any) -> None:
        _live.discard(relay)
        if callback:
            callback(value)

    relay.done.connect(lambda value: finish(on_done, value))
    relay.failed.connect(lambda message: finish(on_error, message))
    QThreadPool.globalInstance().start(_Task(fn, relay))
