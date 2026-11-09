"""
State shared between the display loop (main thread) and the web server threads.

The display driver is only ever touched by the main thread. The web side talks to it
through this object: it asks for a reload / skip, and reads the last image that was shown.
"""
from __future__ import annotations

import io
import threading
import time


class State:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._reload = False
        self._skip = False
        self.png: bytes | None = None
        self.current: dict = {}          # what is on the display now
        self.port: int = 80              # actual web port (may differ from the configured one)

    # ── called from web threads ──────────────────────────────────────────────
    def request_reload(self) -> None:
        with self._lock:
            self._reload = True
        self._event.set()

    def request_skip(self) -> None:
        with self._lock:
            self._skip = True
        self._event.set()

    def snapshot(self) -> tuple[bytes | None, dict]:
        with self._lock:
            return self.png, dict(self.current)

    # ── called from the display loop ─────────────────────────────────────────
    def publish(self, img, info: dict) -> None:
        buf = io.BytesIO()
        img.convert("L").save(buf, format="PNG", optimize=True)
        with self._lock:
            self.png = buf.getvalue()
            self.current = dict(info, shown_at=time.time())

    def wait(self, seconds: float) -> str | None:
        """Sleep up to `seconds`. Returns 'reload', 'next', or None when the time ran out."""
        end = time.monotonic() + seconds
        while True:
            with self._lock:
                if self._reload:
                    self._reload = self._skip = False
                    return "reload"
                if self._skip:
                    self._skip = False
                    return "next"
            left = end - time.monotonic()
            if left <= 0:
                return None
            self._event.wait(left)
            self._event.clear()
