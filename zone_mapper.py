"""
Zone mapping and input decision: maps a smoothed centroid within the ROI to
a 3x3 grid cell and GBA button, debounces so the centroid must settle in a
zone before firing, rate-limits fired inputs, and drives the extended
no-detection "auto-idle" fallback (A-only, per design decision).
"""

import logging
import time

import config

log = logging.getLogger(__name__)


def _index_from_fractions(value_fraction, fractions):
    """Given a 0..1 fraction along an axis and a list of segment-size
    fractions summing to 1.0, return which segment it falls in (clamped to
    the valid range for values right at/past the edges)."""
    cumulative = 0.0
    for i, frac in enumerate(fractions):
        cumulative += frac
        if value_fraction < cumulative:
            return i
    return len(fractions) - 1


def centroid_to_zone(centroid, roi_width, roi_height):
    """Map a centroid (ROI coordinates) to a (row, col) grid cell, using
    config.GRID_COL_FRACTIONS / GRID_ROW_FRACTIONS_BY_COL for non-uniform
    zone sizing (see config.py) rather than assuming equal thirds. Column
    is resolved first, then row within that column's independent row
    split -- see config.py's comment on GRID_ROW_FRACTIONS_BY_COL for why
    this still tiles the ROI with no gaps/overlaps despite each column
    having differently-sized rows."""
    cx, cy = centroid
    col = _index_from_fractions(cx / roi_width, config.GRID_COL_FRACTIONS)
    row = _index_from_fractions(cy / roi_height, config.GRID_ROW_FRACTIONS_BY_COL[col])
    return row, col


def zone_to_button(zone):
    return config.GRID_BUTTON_MAP.get(zone)


class ZoneMapper:
    def __init__(self):
        self._pending_zone = None
        self._pending_count = 0
        self._last_fired_time = 0.0
        self._auto_idle_active = False
        self._last_auto_idle_time = 0.0
        self.auto_idle_activation_count = 0
        # Repeat-input rotation bookkeeping -- see config.REPEAT_INPUT_ROTATION_THRESHOLD.
        # Only touched on a genuinely jellyfish-driven fire (never AUTO-IDLE).
        self._last_fired_key = None  # (button, zone) of the last jellyfish-driven fire
        self._consecutive_repeat_count = 0

    def decide(self, centroid, roi_width, roi_height, seconds_since_qualifying, now: float):
        """
        Given the current smoothed centroid (or None) and how long it's been
        since a jellyfish last qualified as detected, decide whether to fire
        a button this tick.

        Returns a dict:
          {
            "button": str or None,   # button to send this tick, if any
            "zone": (row, col) or None,       # live/raw zone the centroid is
                                               # in *this frame*, independent
                                               # of debounce -- lets the UI
                                               # show a jellyfish "approaching"
                                               # a decision before it's confirmed
            "debounce_progress": float,       # 0..1, how settled the zone is
            "pending_frames": int,            # raw consecutive-frame count
            "auto_idle": bool,       # whether auto-idle fallback is currently active
            "source": "jellyfish" | "auto_idle" | None,
            "force_rotate": bool,    # True the tick a jellyfish-driven fire hits
                                      # config.REPEAT_INPUT_ROTATION_THRESHOLD --
                                      # caller should call tracker.force_rotate()
                                      # and report the outcome back via
                                      # notify_rotation_outcome()
          }
        """
        result = {
            "button": None, "zone": None, "auto_idle": False, "source": None,
            "debounce_progress": 0.0, "pending_frames": 0, "force_rotate": False,
        }

        # Determine auto-idle activation state.
        should_be_idle = (
            seconds_since_qualifying is not None
            and seconds_since_qualifying >= config.AUTO_IDLE_TRIGGER_SEC
        )
        if should_be_idle != self._auto_idle_active:
            self._auto_idle_active = should_be_idle
            if should_be_idle:
                self.auto_idle_activation_count += 1
                log.warning(
                    "AUTO-IDLE fallback activated (no qualifying detection for %.0fs)",
                    seconds_since_qualifying,
                )
                self._last_auto_idle_time = 0.0  # fire soon
            else:
                log.info("AUTO-IDLE fallback deactivated (jellyfish re-detected)")

        result["auto_idle"] = self._auto_idle_active

        if self._auto_idle_active:
            if now - self._last_auto_idle_time >= config.AUTO_IDLE_INTERVAL_SEC:
                self._last_auto_idle_time = now
                self._last_fired_time = now
                result["button"] = config.AUTO_IDLE_BUTTON
                result["source"] = "auto_idle"
                log.info("AUTO-IDLE press: %s", config.AUTO_IDLE_BUTTON)
            return result

        if centroid is None:
            self._pending_zone = None
            self._pending_count = 0
            return result

        zone = centroid_to_zone(centroid, roi_width, roi_height)
        result["zone"] = zone

        if zone == self._pending_zone:
            self._pending_count += 1
        else:
            self._pending_zone = zone
            self._pending_count = 1

        result["pending_frames"] = self._pending_count
        result["debounce_progress"] = min(1.0, self._pending_count / config.DEBOUNCE_FRAMES)

        if self._pending_count < config.DEBOUNCE_FRAMES:
            return result

        if now - self._last_fired_time < config.INPUT_COOLDOWN_SEC:
            return result

        button = zone_to_button(zone)
        if button is None:
            return result

        self._last_fired_time = now
        self._pending_count = 0  # require the zone to re-settle before firing again
        result["button"] = button
        result["source"] = "jellyfish"
        log.info("Input decided: zone=%s -> %s", zone, button)

        fired_key = (button, zone)
        if fired_key == self._last_fired_key:
            self._consecutive_repeat_count += 1
        else:
            self._last_fired_key = fired_key
            self._consecutive_repeat_count = 1
        result["force_rotate"] = self._consecutive_repeat_count >= config.REPEAT_INPUT_ROTATION_THRESHOLD

        return result

    def notify_rotation_outcome(self, switched: bool):
        """Called by main.py right after acting on a decide() result with
        force_rotate=True, reporting whether tracker.force_rotate() actually
        found a different candidate to switch to.

        On success, reset the repeat counter so the newly-locked jellyfish
        gets a fresh count before it can trigger another rotation. On
        failure (no other qualifying candidate existed that frame),
        deliberately leave the counter as-is (at/above threshold) -- since
        it only grows from here, the very next jellyfish-driven fire will
        immediately request another rotation attempt too, rather than
        waiting through a fresh run of REPEAT_INPUT_ROTATION_THRESHOLD
        repeats.
        """
        if switched:
            self._consecutive_repeat_count = 0
            self._last_fired_key = None
