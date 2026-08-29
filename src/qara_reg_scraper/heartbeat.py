"""A background "are you still there?" line for `run`.

Real progress (a document fetched, a source finished) prints on its own,
but between those events there are genuinely slow, silent stretches with
nothing to report yet: a rate-limited wait between requests (e.g.
clearances_510k's default one request per 5s), a big listing/dataset
download (fda:guidance's ~2786-record JSON file), a multi-page openFDA
pagination during `estimate()`, or PoliteHttpClient quietly retrying a
failed request with backoff. None of those have a natural "document done"
event to hang a progress line off of, but a human watching a terminal
should never see more than a few seconds of total silence and wonder if
the process hung.

`Heartbeat` runs a small background thread that prints a dim "still
<doing something>" line if nothing else has printed within `interval`
seconds — call `.beat()` every time real output prints (resets the
silence clock) and `.set_activity(...)` when the phase changes (shown in
the next heartbeat line if the silence threshold is hit before real
progress does). Used as a context manager so its thread's lifetime is
always bounded to one `run` invocation.
"""

from __future__ import annotations

import threading
import time
from typing import Self

DEFAULT_INTERVAL_SECONDS = 5.0


class Heartbeat:
    def __init__(self, console, *, interval: float = DEFAULT_INTERVAL_SECONDS, enabled: bool = True) -> None:
        self._console = console
        self._interval = interval
        self._enabled = enabled
        self._activity = "working"
        self._lock = threading.Lock()
        self._last_beat = time.monotonic()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """Call this whenever real progress prints — resets the silence
        clock so the next heartbeat line is a full `interval` away, not an
        immediate pile-up right after genuine output."""
        with self._lock:
            self._last_beat = time.monotonic()

    def set_activity(self, description: str) -> None:
        """Update what's currently happening, without resetting the
        silence clock — a phase change isn't visible progress on its own;
        it just changes what the *next* heartbeat line (if the silence
        threshold is hit before real progress does) says."""
        with self._lock:
            self._activity = description

    def _run(self) -> None:
        # Poll at a fraction of the configured interval (capped at twice a
        # second so a stop() during a heartbeat's own sleep doesn't add a
        # noticeable shutdown delay, without spinning) — scales down for a
        # short interval (e.g. in tests) instead of missing it entirely by
        # polling coarser than the interval itself.
        poll = min(0.5, self._interval / 4)
        while not self._stop_event.wait(poll):
            with self._lock:
                elapsed = time.monotonic() - self._last_beat
                activity = self._activity
            if elapsed >= self._interval:
                self._console.print(f"  [dim]… still {activity} ({elapsed:.0f}s, no progress printed yet)[/dim]")
                self.beat()  # don't re-print every 0.5s — wait another full interval

    def __enter__(self) -> Self:
        if self._enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
