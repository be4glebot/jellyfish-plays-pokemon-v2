"""
Stream ingestion: resolves the YouTube live URL to a direct stream URL via
yt-dlp, opens it with OpenCV, and runs capture in a background thread.

Frames are held in an elastic playback buffer and released to consumers at
a steady, deliberately-below-average pace (config.PLAYBACK_INTERVAL_SEC),
allowed to build up to config.PLAYBACK_BUFFER_SEC of backlog -- see the
comments on those in config.py for why: this stream stalls for several
seconds at a time fairly often, and under-consuming the average arrival
rate (not just delaying everything by a fixed amount, which doesn't
actually help -- see config.py) is what lets a video player stay smooth
through that instead of freezing, at the cost of always running a bit
behind the live tank.
"""

import collections
import logging
import os
import subprocess
import sys
import threading
import time

import cv2

import config

log = logging.getLogger(__name__)

# Tell OpenCV's FFmpeg backend to avoid internal buffering/frame reordering
# on network reads -- without this, FFmpeg queues up several seconds of
# frames before handing them to cv2.VideoCapture.read(), so every read()
# returns an already-stale frame and the whole pipeline (video feed +
# button-decision debounce, which counts loop iterations) lags behind the
# live stream by however much got buffered. Must be set before the first
# cv2.VideoCapture(...) call to take effect.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "fflags;nobuffer|flags;low_delay|reorder_queue_size;0",
)


class _ExpectedReconnect(RuntimeError):
    """A reconnect triggered by one of this stream's known, self-recovering
    quirks (a stuck read, a stale connection) -- see config.py's comments on
    STREAM_READ_TIMEOUT_SEC/STREAM_STALE_TIMEOUT_SEC for why these happen
    routinely (every 10-20s) and aren't actually a problem. Logged at INFO
    instead of WARNING so they don't drown out genuinely unexpected failures
    (yt-dlp resolution failing, the capture never opening at all) in the
    dashboard's error log, which only holds the last 20 entries."""


def resolve_stream_url(page_url: str) -> str:
    """Resolve a YouTube watch URL to a direct playable stream URL via yt-dlp.

    Invoked as `sys.executable -m yt_dlp` rather than the bare `yt-dlp`
    console-script executable. Confirmed via manual testing that the bare
    `yt-dlp` entry point can silently fail (non-zero exit, empty output) in
    some environments even when yt-dlp is correctly installed and on PATH,
    while `-m yt_dlp` against the same interpreter this script is already
    running under works reliably -- it also sidesteps any PATH/activation
    mismatch between the venv and the shell subprocess.run() inherits.
    """
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "-g", "-f", config.STREAM_FORMAT_SELECTOR, page_url],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed to resolve stream: {result.stderr.strip()}")
    urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not urls:
        raise RuntimeError("yt-dlp returned no stream URL")
    # "-f best" should yield a single combined URL; take the first line.
    return urls[0]


class StreamCapture:
    """
    Runs YouTube stream resolution + OpenCV capture in a background thread.
    Consumers call get_latest_frame() to pull frames one at a time from an
    elastic playback buffer, paced at config.PLAYBACK_INTERVAL_SEC and
    allowed to build up to config.PLAYBACK_BUFFER_SEC deep -- see module
    docstring and config.py for why a plain FIFO/delay isn't enough and
    this needs to actively under-consume the arrival rate. Single-consumer:
    strictly FIFO, one caller (main.py's loop) assumed, not several.
    """

    def __init__(self, page_url: str = config.STREAM_URL):
        self.page_url = page_url
        # (capture_timestamp, frame) tuples, oldest first, popped on serve
        # -- see _push_frame()/get_latest_frame().
        self._buffer: "collections.deque" = collections.deque()
        self._buffer_cv = threading.Condition()
        # None until the initial cushion (PLAYBACK_BUFFER_SEC deep) has
        # built up; then the monotonic time the next frame should release.
        self._next_release_time = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "stopped"  # stopped | connecting | connected | reconnecting | failed
        self._state_lock = threading.Lock()

    # -- public API ---------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="StreamCapture", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._set_state("stopped")
        with self._buffer_cv:
            self._buffer.clear()
            self._next_release_time = None
            self._buffer_cv.notify_all()

    def get_latest_frame(self, timeout: float = 1.0):
        """Return the next frame from the elastic playback buffer, blocking
        up to `timeout` seconds for one to become due. Returns None if none
        did in time (e.g. still filling the initial cushion at startup, or
        a capture stall has outlasted the accumulated backlog)."""
        deadline = time.monotonic() + timeout
        with self._buffer_cv:
            while True:
                now = time.monotonic()

                if self._next_release_time is None:
                    # Still building the initial cushion -- don't start
                    # playback until we have a full PLAYBACK_BUFFER_SEC of
                    # backlog banked, so the very first stall doesn't
                    # immediately run the buffer dry.
                    span = self._buffer[-1][0] - self._buffer[0][0] if len(self._buffer) >= 2 else 0.0
                    if span >= config.PLAYBACK_BUFFER_SEC:
                        self._next_release_time = now
                    else:
                        remaining = deadline - now
                        if remaining <= 0:
                            return None
                        self._buffer_cv.wait(timeout=min(remaining, 0.1))
                        continue

                if now >= self._next_release_time and self._buffer:
                    _ts, frame = self._buffer.popleft()
                    self._next_release_time += config.PLAYBACK_INTERVAL_SEC
                    # If playback fell behind (buffer was empty for a
                    # while), don't let it try to catch up with a burst --
                    # resume pacing from now instead of racing through
                    # whatever's left.
                    if self._next_release_time < now:
                        self._next_release_time = now
                    return frame

                remaining = deadline - now
                if remaining <= 0:
                    return None
                wait_for = min(remaining, 0.1)
                if self._next_release_time > now:
                    wait_for = min(wait_for, self._next_release_time - now)
                self._buffer_cv.wait(timeout=max(wait_for, 0.0))

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    # -- internal -------------------------------------------------------

    def _set_state(self, state: str):
        with self._state_lock:
            if self._state != state:
                log.info("Stream state: %s -> %s", self._state, state)
                self._state = state

    def _run(self):
        while not self._stop_event.is_set():
            cap = None
            abandoned = False
            try:
                self._set_state("connecting")
                direct_url = resolve_stream_url(self.page_url)
                cap = cv2.VideoCapture(direct_url)
                if not cap.isOpened():
                    raise RuntimeError("cv2.VideoCapture failed to open resolved stream URL")
                # Keep OpenCV's own internal buffer as small as possible too
                # (backend-dependent whether this is honored, but harmless
                # when it isn't) -- same goal as OPENCV_FFMPEG_CAPTURE_OPTIONS
                # above: always read the freshest frame, not a queued one.
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                self._set_state("connected")
                last_frame_time = time.monotonic()

                while not self._stop_event.is_set():
                    ok, frame, done_event = self._read_with_timeout(
                        cap, config.STREAM_READ_TIMEOUT_SEC
                    )
                    now = time.monotonic()

                    if done_event is not None and not done_event.is_set():
                        # cap.read() itself has blocked past the timeout --
                        # see config.STREAM_READ_TIMEOUT_SEC for why. Hand
                        # this capture off to a cleanup thread (its read()
                        # may still be in flight; releasing it here would
                        # race that) and reconnect with a freshly resolved
                        # URL instead of continuing to wait on this one.
                        self._abandon_capture(cap, done_event)
                        abandoned = True
                        raise _ExpectedReconnect(
                            f"cap.read() blocked >{config.STREAM_READ_TIMEOUT_SEC}s, "
                            "forcing reconnect"
                        )

                    if not ok or frame is None:
                        if now - last_frame_time > config.STREAM_STALE_TIMEOUT_SEC:
                            raise _ExpectedReconnect(
                                f"No frames for >{config.STREAM_STALE_TIMEOUT_SEC}s, forcing reconnect"
                            )
                        time.sleep(0.05)
                        continue

                    last_frame_time = now
                    self._push_frame(frame)

            except Exception as exc:
                if self._stop_event.is_set():
                    break
                self._set_state("reconnecting")
                log_fn = log.info if isinstance(exc, _ExpectedReconnect) else log.warning
                log_fn("Stream capture error: %s", exc)
                time.sleep(config.STREAM_RECONNECT_BACKOFF_SEC)
            finally:
                if cap is not None and not abandoned:
                    cap.release()

        self._set_state("stopped")

    @staticmethod
    def _read_with_timeout(cap, timeout_sec):
        """Run cap.read() in a daemon thread and wait up to timeout_sec for
        it to finish. Returns (ok, frame, done_event). On success, done_event
        is already set. On timeout, ok/frame are None and done_event is
        still unset -- the read is still running in the background; pass
        done_event to _abandon_capture() so its eventual cleanup waits for
        that specific call to actually finish rather than racing it."""
        result = {}
        done = threading.Event()

        def _do_read():
            try:
                result["ok"], result["frame"] = cap.read()
            except Exception as exc:
                result["exc"] = exc
            finally:
                done.set()

        threading.Thread(target=_do_read, daemon=True).start()
        if not done.wait(timeout_sec):
            return None, None, done
        if "exc" in result:
            raise result["exc"]
        return result.get("ok"), result.get("frame"), done

    @staticmethod
    def _abandon_capture(cap, done_event):
        """Release a capture whose cap.read() call is still in flight, but
        only once that call actually returns -- calling cap.release() while
        a read() is concurrently in progress on the same object is not
        safe. If the read never returns, this capture is simply leaked for
        the process lifetime rather than risking a release-under-read race."""
        def _wait_and_release():
            done_event.wait()
            try:
                cap.release()
            except Exception:
                pass

        threading.Thread(target=_wait_and_release, daemon=True).start()

    def _push_frame(self, frame):
        now = time.monotonic()
        with self._buffer_cv:
            self._buffer.append((now, frame))
            # Frames are normally drained by get_latest_frame() as they
            # become due, so this deque should never hold much more than
            # PLAYBACK_BUFFER_SEC worth of backlog -- this is just a safety
            # cap (generously above that) in case the consumer stalls for
            # a while, so memory can't grow unbounded.
            cutoff = now - (config.PLAYBACK_BUFFER_SEC * 3)
            while self._buffer and self._buffer[0][0] < cutoff:
                self._buffer.popleft()
            self._buffer_cv.notify_all()
