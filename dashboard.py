"""
Dashboard: two pages sharing one SharedState instance (written by main.py's
processing loop, read here) so no tracking logic is duplicated in the web
layer.

- "/"       Primary OBS browser-source page: the live ROI-cropped tracking
            feed with a 3x3 grid of button icons overlaid directly on top,
            styled after the classic "Fish Plays Pokemon" look. The cell
            the jellyfish is currently drifting into glows in proportion to
            debounce progress, so viewers see a decision "building" before
            it fires, not just after.
- "/status" Denser operational view: exact centroid/zone/debounce state,
            connection health, recent errors, session stats, and the
            scrolling input log.

Both pull from the same /status.json endpoint.
"""

import logging
import threading
import time
from collections import deque

import cv2
from flask import Flask, Response, jsonify, render_template_string

import config

log = logging.getLogger(__name__)


# Button -> overlay presentation. "kind" drives the CSS class used to style
# the cell (arrow / circle / pill / minor); see PRIMARY_TEMPLATE's <style>.
BUTTON_STYLE = {
    "UP": {"kind": "arrow", "label": "↑"},
    "DOWN": {"kind": "arrow", "label": "↓"},
    "LEFT": {"kind": "arrow", "label": "←"},
    "RIGHT": {"kind": "arrow", "label": "→"},
    "A": {"kind": "circle", "label": "A", "bg": "#e8158f"},
    "B": {"kind": "circle", "label": "B", "bg": "#3d4a68"},
    "START": {"kind": "pill", "label": "START"},
    "SELECT": {"kind": "pill", "label": "SELECT"},
    "L": {"kind": "minor", "label": "L"},
    "R": {"kind": "minor", "label": "R"},
}


def _grid_cells():
    """Build the 9 grid cells (row, col, button, style, position) from
    config, so a remapped GRID_BUTTON_MAP or re-tuned zone sizing is
    reflected automatically without template changes.

    Cells are non-uniformly sized (see config.GRID_COL_FRACTIONS /
    GRID_ROW_FRACTIONS_BY_COL) so the visual overlay honestly reflects each
    button's actual trigger-area size instead of a cosmetic equal-thirds
    grid -- left/top/width/height are precomputed here as percentages
    since CSS Grid can't express independent per-column row splits
    ("ragged" rows), so the template positions cells absolutely instead.
    """
    cells = []
    col_left = 0.0
    for col in range(config.GRID_COLS):
        col_width = config.GRID_COL_FRACTIONS[col]
        row_fractions = config.GRID_ROW_FRACTIONS_BY_COL[col]
        row_top = 0.0
        for row in range(config.GRID_ROWS):
            row_height = row_fractions[row]
            button = config.GRID_BUTTON_MAP.get((row, col))
            style = BUTTON_STYLE.get(button, {"kind": "minor", "label": button or "?"})
            cells.append({
                "row": row, "col": col, "button": button,
                "left": round(col_left * 100, 3),
                "top": round(row_top * 100, 3),
                "width": round(col_width * 100, 3),
                "height": round(row_height * 100, 3),
                **style,
            })
            row_top += row_height
        col_left += col_width
    return cells


class ErrorLogHandler(logging.Handler):
    """Feeds WARNING+ log records app-wide into the dashboard's recent-errors
    list, so /status can show real errors without each module hand-rolling
    its own error reporting into SharedState."""

    def __init__(self, shared_state: "SharedState"):
        super().__init__(level=logging.WARNING)
        self.shared_state = shared_state

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.shared_state.record_error(msg, record.levelname)


class SharedState:
    """Thread-safe holder for everything both dashboard pages render."""

    def __init__(self):
        self._lock = threading.Lock()

        self._frame_roi_bytes: bytes | None = None
        self._frame_full_bytes: bytes | None = None
        # Bumped on every set_frame_roi() call -- lets the MJPEG generator
        # (and diagnostics) distinguish "the producer pushed a genuinely new
        # frame" from "we're re-sending the same bytes because nothing new
        # has arrived yet", which a raw parts-sent-per-second count can't.
        self._frame_roi_seq = 0

        self.start_time = time.time()

        self.current_button = None
        self.current_zone = None          # last zone a real input fired from
        self.raw_zone = None              # live zone this frame, pre-debounce
        self.debounce_progress = 0.0
        self.pending_frames = 0
        self.centroid = None              # smoothed centroid, ROI coords

        self.auto_idle = False
        self.auto_idle_activation_count = 0

        self.stream_state = "stopped"
        self.mgba_connected = False
        self.fps = 0.0
        self.latency_ms = 0.0
        self.last_frame_time = None

        self.total_inputs = 0
        self.inputs_by_button = {}
        self.last_input_time = None

        self._seq = 0
        self.input_log = deque(maxlen=config.DASHBOARD_LOG_LENGTH)
        self.error_log = deque(maxlen=20)
        self.rotation_log = deque(maxlen=10)

    # -- writers (called from main.py's loop / logging) -------------------

    def set_frame_roi(self, frame_bgr):
        ok, buf = cv2.imencode(
            ".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), config.DASHBOARD_MJPEG_QUALITY],
        )
        if ok:
            with self._lock:
                self._frame_roi_bytes = buf.tobytes()
                self._frame_roi_seq += 1

    def get_frame_roi_seq(self):
        with self._lock:
            return self._frame_roi_seq

    def set_frame_full(self, frame_bgr):
        ok, buf = cv2.imencode(
            ".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), config.DASHBOARD_MJPEG_QUALITY],
        )
        if ok:
            with self._lock:
                self._frame_full_bytes = buf.tobytes()

    def get_frame_roi_bytes(self):
        with self._lock:
            return self._frame_roi_bytes

    def get_frame_full_bytes(self):
        with self._lock:
            return self._frame_full_bytes

    def record_input(self, button: str, source: str, zone):
        with self._lock:
            self._seq += 1
            self.current_button = button
            self.last_input_time = time.time()
            self.total_inputs += 1
            self.inputs_by_button[button] = self.inputs_by_button.get(button, 0) + 1
            self.input_log.appendleft({
                "seq": self._seq,
                "time": time.strftime("%H:%M:%S"),
                "button": button,
                "zone": list(zone) if zone else None,
                "source": source,
            })

    def record_rotation(self, old_centroid, new_centroid, button, zone):
        """A forced-rotation event (see config.REPEAT_INPUT_ROTATION_THRESHOLD)
        -- distinct from input_log, since this isn't a fired button, it's the
        tracker switching which jellyfish it's locked onto."""
        with self._lock:
            self.rotation_log.appendleft({
                "time": time.strftime("%H:%M:%S"),
                "button": button,
                "zone": list(zone) if zone else None,
                "old_centroid": [round(old_centroid[0], 1), round(old_centroid[1], 1)] if old_centroid else None,
                "new_centroid": [round(new_centroid[0], 1), round(new_centroid[1], 1)] if new_centroid else None,
            })

    def record_error(self, message: str, level: str):
        with self._lock:
            self.error_log.appendleft({
                "time": time.strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            })

    def update_status(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self):
        with self._lock:
            now = time.time()
            return {
                "current_button": self.current_button,
                "current_zone": self.current_zone,
                "raw_zone": self.raw_zone,
                "debounce_progress": round(self.debounce_progress, 3),
                "pending_frames": self.pending_frames,
                "debounce_frames_required": config.DEBOUNCE_FRAMES,
                "centroid": [round(self.centroid[0], 1), round(self.centroid[1], 1)] if self.centroid else None,
                "auto_idle": self.auto_idle,
                "auto_idle_activation_count": self.auto_idle_activation_count,
                "stream_state": self.stream_state,
                "mgba_connected": self.mgba_connected,
                "fps": round(self.fps, 1),
                "latency_ms": round(self.latency_ms, 1),
                "last_frame_age_sec": round(now - self.last_frame_time, 1) if self.last_frame_time else None,
                "uptime_sec": round(now - self.start_time),
                "total_inputs": self.total_inputs,
                "inputs_by_button": dict(self.inputs_by_button),
                "last_input_age_sec": round(now - self.last_input_time, 1) if self.last_input_time else None,
                "input_log": list(self.input_log),
                "rotation_log": list(self.rotation_log),
                "error_log": list(self.error_log),
                "grid": {"rows": config.GRID_ROWS, "cols": config.GRID_COLS},
            }


# --------------------------------------------------------------------------
# Primary page: video + grid overlay
# --------------------------------------------------------------------------

PRIMARY_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Jellyfish Plays Pokemon</title>
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; background: #000; color: #fff;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    width: 100%; height: 100%; overflow: hidden;
  }
  #stage { position: relative; width: 100%; height: 100%; background: #000; }
  #stage img { width: 100%; height: 100%; object-fit: contain; display: block; }
  #gridOverlay {
    position: absolute;
    pointer-events: none;
  }
  .cell {
    position: absolute;
    border: 1px solid rgba(255,255,255,0.55);
    display: flex; align-items: center; justify-content: center;
    transition: background 120ms ease, box-shadow 120ms ease;
    box-sizing: border-box;
  }
  .cell.minor { border-color: rgba(255,255,255,0.25); }
  .arrow-icon {
    font-size: 8vw; font-weight: 900; color: rgba(255,255,255,0.65);
    text-shadow: 0 0 6px rgba(0,0,0,0.85), 0 0 2px rgba(0,0,0,0.9);
  }
  .circle-badge {
    width: 16%; aspect-ratio: 1; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 3.4vw; font-weight: 800; color: #fff;
    box-shadow: 0 0 10px rgba(0,0,0,0.6);
    opacity: 0.75;
  }
  .pill-badge {
    padding: 0.5vw 1.6vw; border-radius: 999px;
    background: rgba(10,12,24,0.6); color: #fff;
    font-size: 1.6vw; font-weight: 700; letter-spacing: 0.08em;
    opacity: 0.8;
  }
  .minor-label {
    font-size: 1.6vw; font-weight: 600; color: rgba(255,255,255,0.4);
    position: absolute; bottom: 6%; right: 8%;
  }
  .cell.approaching {
    background: rgba(255, 215, 90, var(--glow, 0));
    box-shadow: inset 0 0 calc(6px + 40px * var(--glow, 0)) rgba(255, 215, 90, var(--glow, 0));
  }
  .cell.approaching .arrow-icon,
  .cell.approaching .pill-badge,
  .cell.approaching .circle-badge { opacity: 1; }
  .cell.just-fired {
    animation: flash 400ms ease-out;
  }
  @keyframes flash {
    0% { background: rgba(255,255,255,0.9); }
    100% { background: rgba(255,255,255,0); }
  }
  #hud {
    position: absolute; top: 10px; left: 10px;
    background: rgba(10,14,30,0.72); border-radius: 8px;
    padding: 6px 12px; font-size: 14px; line-height: 1.5;
    pointer-events: none;
  }
  .badge-tag {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-weight: 700; font-size: 12px; margin-left: 6px; vertical-align: middle;
  }
  .badge-tag.on { background: #ffb300; color: #201400; }
  .badge-tag.off { background: #2a3050; color: #8f9ac0; }
  #footer {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(10,14,30,0.72); padding: 4px 10px; font-size: 12px;
    display: flex; gap: 6px; overflow: hidden; pointer-events: none;
  }
  #footer span { background: rgba(255,255,255,0.1); border-radius: 6px; padding: 2px 8px; white-space: nowrap; }
  a.statuslink {
    position: absolute; top: 10px; right: 10px; color: rgba(255,255,255,0.5);
    font-size: 12px; text-decoration: none; background: rgba(10,14,30,0.6);
    padding: 4px 8px; border-radius: 6px;
  }
</style>
</head>
<body>
  <div id="stage">
    <img id="video" src="/video_feed_roi" alt="jellyfish tracking feed">
    <div id="gridOverlay">
      {% for cell in cells %}
      <div class="cell {{ cell.kind }}" data-row="{{ cell.row }}" data-col="{{ cell.col }}"
           style="left:{{ cell.left }}%; top:{{ cell.top }}%; width:{{ cell.width }}%; height:{{ cell.height }}%;">
        {% if cell.kind == 'arrow' %}<div class="arrow-icon">{{ cell.label }}</div>
        {% elif cell.kind == 'circle' %}<div class="circle-badge" style="background:{{ cell.bg }}">{{ cell.label }}</div>
        {% elif cell.kind == 'pill' %}<div class="pill-badge">{{ cell.label }}</div>
        {% else %}<div class="minor-label">{{ cell.label }}</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    <div id="hud">Button: <b id="hudButton">-</b><span id="hudIdle" class="badge-tag off">AUTO-IDLE</span></div>
    <a class="statuslink" href="/status">stream info &rarr;</a>
    <div id="footer" id="log"></div>
  </div>
<script>
const ROWS = {{ rows }}, COLS = {{ cols }};
let lastSeq = 0;

function layoutOverlay() {
  const img = document.getElementById('video');
  const stage = document.getElementById('stage');
  const overlay = document.getElementById('gridOverlay');
  const cw = stage.clientWidth, ch = stage.clientHeight;
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) { overlay.style.left = '0px'; overlay.style.top='0px'; overlay.style.width=cw+'px'; overlay.style.height=ch+'px'; return; }
  const containerRatio = cw / ch, imgRatio = nw / nh;
  let width, height;
  if (imgRatio > containerRatio) { width = cw; height = cw / imgRatio; }
  else { height = ch; width = ch * imgRatio; }
  overlay.style.left = ((cw - width) / 2) + 'px';
  overlay.style.top = ((ch - height) / 2) + 'px';
  overlay.style.width = width + 'px';
  overlay.style.height = height + 'px';
}

function cellAt(row, col) {
  return document.querySelector('.cell[data-row="'+row+'"][data-col="'+col+'"]');
}

async function poll() {
  try {
    const r = await fetch('/status.json');
    const s = await r.json();

    document.getElementById('hudButton').textContent = s.current_button || '-';
    const idle = document.getElementById('hudIdle');
    idle.className = 'badge-tag ' + (s.auto_idle ? 'on' : 'off');

    document.querySelectorAll('.cell').forEach(c => {
      c.classList.remove('approaching');
      c.style.removeProperty('--glow');
    });
    if (s.raw_zone) {
      const [row, col] = s.raw_zone;
      const cell = cellAt(row, col);
      if (cell) {
        cell.classList.add('approaching');
        cell.style.setProperty('--glow', s.debounce_progress);
      }
    }

    for (const entry of s.input_log) {
      if (entry.seq > lastSeq && entry.zone) {
        const cell = cellAt(entry.zone[0], entry.zone[1]);
        if (cell) {
          cell.classList.add('just-fired');
          setTimeout(() => cell.classList.remove('just-fired'), 420);
        }
      }
    }
    if (s.input_log.length) lastSeq = Math.max(lastSeq, ...s.input_log.map(e => e.seq));

    const footer = document.getElementById('log');
    footer.innerHTML = s.input_log.slice(0, 8).map(e =>
      '<span>' + e.time + ' ' + e.button + (e.source === 'auto_idle' ? '*' : '') + '</span>'
    ).join('');
  } catch (e) { /* transient poll failure, ignore */ }
}

window.addEventListener('resize', layoutOverlay);
document.getElementById('video').addEventListener('load', layoutOverlay);
setInterval(layoutOverlay, 500);
setInterval(poll, 200);
layoutOverlay();
poll();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Overlay page: grid + HUD only, transparent background, no video feed --
# meant to run inside a transparent, click-through, always-on-top Electron
# window (see overlay_app/) stacked on top of a separate browser window
# playing the raw YouTube stream directly, instead of re-encoding/streaming
# a cropped, throttled copy of it through this Flask process. See
# overlay_app/main.js for how the two windows are kept in sync.
#
# Since the video underneath is the *full, uncropped* stream (not the
# ROI-cropped /video_feed_roi image the primary page overlays), the grid
# here is confined to a centered box sized to config.ROI_WIDTH_FRACTION /
# ROI_HEIGHT_FRACTION -- the same region tracker.py/zone_mapper.py actually
# operate on -- rather than filling the whole stage.
# --------------------------------------------------------------------------

OVERLAY_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Jellyfish Overlay</title>
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; background: transparent; color: #fff;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    width: 100%; height: 100%; overflow: hidden;
  }
  #stage { position: relative; width: 100%; height: 100%; }
  #gridOverlay {
    position: absolute;
    left: {{ roi_left }}%; top: {{ roi_top }}%;
    width: {{ roi_width }}%; height: {{ roi_height }}%;
    pointer-events: none;
  }
  .cell {
    position: absolute;
    border: 1px solid rgba(255,255,255,0.55);
    display: flex; align-items: center; justify-content: center;
    transition: background 120ms ease, box-shadow 120ms ease;
    box-sizing: border-box;
  }
  .cell.minor { border-color: rgba(255,255,255,0.25); }
  .arrow-icon {
    font-size: 8vw; font-weight: 900; color: rgba(255,255,255,0.65);
    text-shadow: 0 0 6px rgba(0,0,0,0.85), 0 0 2px rgba(0,0,0,0.9);
  }
  .circle-badge {
    width: 16%; aspect-ratio: 1; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 3.4vw; font-weight: 800; color: #fff;
    box-shadow: 0 0 10px rgba(0,0,0,0.6);
    opacity: 0.75;
  }
  .pill-badge {
    padding: 0.5vw 1.6vw; border-radius: 999px;
    background: rgba(10,12,24,0.6); color: #fff;
    font-size: 1.6vw; font-weight: 700; letter-spacing: 0.08em;
    opacity: 0.8;
  }
  .minor-label {
    font-size: 1.6vw; font-weight: 600; color: rgba(255,255,255,0.4);
    position: absolute; bottom: 6%; right: 8%;
  }
  .cell.approaching {
    background: rgba(255, 215, 90, var(--glow, 0));
    box-shadow: inset 0 0 calc(6px + 40px * var(--glow, 0)) rgba(255, 215, 90, var(--glow, 0));
  }
  .cell.approaching .arrow-icon,
  .cell.approaching .pill-badge,
  .cell.approaching .circle-badge { opacity: 1; }
  .cell.just-fired {
    animation: flash 400ms ease-out;
  }
  @keyframes flash {
    0% { background: rgba(255,255,255,0.9); }
    100% { background: rgba(255,255,255,0); }
  }
  #hud {
    position: absolute; top: 10px; left: 10px;
    background: rgba(10,14,30,0.72); border-radius: 8px;
    padding: 6px 12px; font-size: 14px; line-height: 1.5;
    pointer-events: none;
  }
  .badge-tag {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-weight: 700; font-size: 12px; margin-left: 6px; vertical-align: middle;
  }
  .badge-tag.on { background: #ffb300; color: #201400; }
  .badge-tag.off { background: #2a3050; color: #8f9ac0; }
  #footer {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(10,14,30,0.72); padding: 4px 10px; font-size: 12px;
    display: flex; gap: 6px; overflow: hidden; pointer-events: none;
  }
  #footer span { background: rgba(255,255,255,0.1); border-radius: 6px; padding: 2px 8px; white-space: nowrap; }
</style>
</head>
<body>
  <div id="stage">
    <div id="gridOverlay">
      {% for cell in cells %}
      <div class="cell {{ cell.kind }}" data-row="{{ cell.row }}" data-col="{{ cell.col }}"
           style="left:{{ cell.left }}%; top:{{ cell.top }}%; width:{{ cell.width }}%; height:{{ cell.height }}%;">
        {% if cell.kind == 'arrow' %}<div class="arrow-icon">{{ cell.label }}</div>
        {% elif cell.kind == 'circle' %}<div class="circle-badge" style="background:{{ cell.bg }}">{{ cell.label }}</div>
        {% elif cell.kind == 'pill' %}<div class="pill-badge">{{ cell.label }}</div>
        {% else %}<div class="minor-label">{{ cell.label }}</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    <div id="hud">Button: <b id="hudButton">-</b><span id="hudIdle" class="badge-tag off">AUTO-IDLE</span></div>
    <div id="footer" id="log"></div>
  </div>
<script>
let lastSeq = 0;

function cellAt(row, col) {
  return document.querySelector('.cell[data-row="'+row+'"][data-col="'+col+'"]');
}

async function poll() {
  try {
    const r = await fetch('/status.json');
    const s = await r.json();

    document.getElementById('hudButton').textContent = s.current_button || '-';
    const idle = document.getElementById('hudIdle');
    idle.className = 'badge-tag ' + (s.auto_idle ? 'on' : 'off');

    document.querySelectorAll('.cell').forEach(c => {
      c.classList.remove('approaching');
      c.style.removeProperty('--glow');
    });
    if (s.raw_zone) {
      const [row, col] = s.raw_zone;
      const cell = cellAt(row, col);
      if (cell) {
        cell.classList.add('approaching');
        cell.style.setProperty('--glow', s.debounce_progress);
      }
    }

    for (const entry of s.input_log) {
      if (entry.seq > lastSeq && entry.zone) {
        const cell = cellAt(entry.zone[0], entry.zone[1]);
        if (cell) {
          cell.classList.add('just-fired');
          setTimeout(() => cell.classList.remove('just-fired'), 420);
        }
      }
    }
    if (s.input_log.length) lastSeq = Math.max(lastSeq, ...s.input_log.map(e => e.seq));

    const footer = document.getElementById('log');
    footer.innerHTML = s.input_log.slice(0, 8).map(e =>
      '<span>' + e.time + ' ' + e.button + (e.source === 'auto_idle' ? '*' : '') + '</span>'
    ).join('');
  } catch (e) { /* transient poll failure, ignore */ }
}

setInterval(poll, 200);
poll();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Status page: operational details
# --------------------------------------------------------------------------

STATUS_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Jellyfish Stats</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0b1020; color: #e8ecff;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif; font-size: 13px;
    padding: 16px;
  }
  h1 { font-size: 14px; margin: 0; font-weight: 700; }
  h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: #8f9ac0; margin: 16px 0 6px; font-weight: 700; }
  h2 .hint { text-transform: none; letter-spacing: normal; font-weight: 400; }
  .muted { color: #8f9ac0; }

  .topbar { display: flex; align-items: center; justify-content: space-between; }
  .topbar .status { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .dot.ok { background: #3ddc84; } .dot.bad { background: #ff5c5c; }

  .now-card {
    background: linear-gradient(160deg, #182246, #131a33);
    border-radius: 12px; padding: 14px 16px; margin-top: 12px;
    display: flex; align-items: flex-start; justify-content: space-between;
  }
  .now-button { font-size: 30px; font-weight: 800; line-height: 1; }
  .now-zone { font-size: 12px; color: #8f9ac0; margin-top: 5px; }
  .badge { padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }
  .badge.on { background: #ffb300; color: #201400; }
  .badge.off { background: #232a4a; color: #7fb2ff; }

  .bar { height: 5px; border-radius: 4px; background: #1c2340; margin-top: 10px; overflow: hidden; }
  .bar-fill { height: 100%; background: #7fb2ff; width: 0%; transition: width 150ms linear; }
  .now-sub { font-size: 11px; color: #8f9ac0; margin-top: 5px; }

  .tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 14px; }
  .tile { background: #131a33; border-radius: 8px; padding: 8px 10px; }
  .tile-label { font-size: 10px; color: #8f9ac0; text-transform: uppercase; letter-spacing: 0.04em; }
  .tile-value { font-size: 15px; font-weight: 700; margin-top: 2px; }
  .tile-value.ok { color: #3ddc84; } .tile-value.bad { color: #ff5c5c; }

  .card { background: #131a33; border-radius: 8px; padding: 8px 10px; }

  .bbar-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
  .bbar-label { width: 44px; color: #8f9ac0; flex-shrink: 0; }
  .bbar-track { flex: 1; height: 8px; border-radius: 4px; background: #1c2340; overflow: hidden; }
  .bbar-fill { height: 100%; background: #7fb2ff; }
  .bbar-count { width: 22px; text-align: right; font-weight: 600; flex-shrink: 0; }

  .list { max-height: 180px; overflow-y: auto; }
  .list-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .list-row:last-child { border-bottom: none; }
  .list-time { color: #8f9ac0; width: 52px; flex-shrink: 0; }
  .tag { padding: 1px 7px; border-radius: 999px; font-size: 10px; font-weight: 700; flex-shrink: 0; white-space: nowrap; }
  .tag.error { background: #3a1620; color: #ff6b6b; }
  .tag.warning { background: #3a2c14; color: #ffcf5c; }
  .tag.rotation { background: #241a3a; color: #b39dff; }
  .tag.jellyfish { background: #142a1c; color: #6fe3a0; }
  .tag.auto_idle { background: #2a2414; color: #ffcf5c; }
  .list-msg { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
  <div class="topbar">
    <h1>Jellyfish Stats</h1>
    <div class="status"><span class="dot" id="streamDot"></span><span class="muted" id="streamState">-</span></div>
  </div>

  <div class="now-card">
    <div>
      <div class="now-button" id="hudButton">-</div>
      <div class="now-zone" id="zoneLine">-</div>
    </div>
    <span class="badge off" id="autoIdleBadge">TRACKING</span>
  </div>
  <div class="bar"><div class="bar-fill" id="debounceBar"></div></div>
  <div class="now-sub"><span id="debounceText">-</span> &middot; last input <span id="lastInputAge">-</span></div>

  <div class="tiles">
    <div class="tile"><div class="tile-label">FPS</div><div class="tile-value" id="fps">-</div></div>
    <div class="tile"><div class="tile-label">Latency</div><div class="tile-value" id="latency">-</div></div>
    <div class="tile"><div class="tile-label">Uptime</div><div class="tile-value" id="uptime">-</div></div>
    <div class="tile"><div class="tile-label">Total inputs</div><div class="tile-value" id="totalInputs">-</div></div>
    <div class="tile"><div class="tile-label">mGBA link</div><div class="tile-value" id="mgbaState">-</div></div>
    <div class="tile"><div class="tile-label">Idle activations</div><div class="tile-value" id="idleActivations">-</div></div>
  </div>

  <h2>Inputs by button</h2>
  <div class="card" id="buttonBreakdown"><span class="muted">no inputs fired yet</span></div>

  <h2>Events <span class="hint">errors &amp; forced rotations (after {{ rotation_threshold }} repeats)</span></h2>
  <div class="card list" id="eventsLog"><div class="muted">no events yet</div></div>

  <h2>Recent inputs</h2>
  <div class="card list" id="inputsLog"><div class="muted">no inputs fired yet</div></div>

<script>
function fmtZone(z) { return z ? '(' + z[0] + ',' + z[1] + ')' : '-'; }
function fmtAge(s) { return (s === null || s === undefined) ? '-' : (s < 60 ? s.toFixed(1)+'s ago' : Math.floor(s/60)+'m '+Math.floor(s%60)+'s ago'); }
function fmtUptime(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
  return (h?h+'h ':'') + m + 'm ' + sec + 's';
}

async function poll() {
  try {
    const r = await fetch('/status.json');
    const s = await r.json();

    document.getElementById('streamState').textContent = s.stream_state;
    document.getElementById('streamDot').className = 'dot ' + (s.stream_state === 'connected' ? 'ok' : 'bad');

    document.getElementById('hudButton').textContent = s.current_button || '-';
    document.getElementById('zoneLine').textContent = 'zone ' + fmtZone(s.raw_zone)
      + (s.current_zone ? ' · last fired ' + fmtZone(s.current_zone) : '');
    const idleBadge = document.getElementById('autoIdleBadge');
    idleBadge.className = 'badge ' + (s.auto_idle ? 'on' : 'off');
    idleBadge.textContent = s.auto_idle ? 'AUTO-IDLE' : 'TRACKING';
    document.getElementById('debounceBar').style.width = Math.round(s.debounce_progress * 100) + '%';
    document.getElementById('debounceText').textContent = s.pending_frames + '/' + s.debounce_frames_required + ' frames';
    document.getElementById('lastInputAge').textContent = fmtAge(s.last_input_age_sec);

    document.getElementById('fps').textContent = s.fps + ' fps';
    document.getElementById('latency').textContent = s.latency_ms + ' ms';
    document.getElementById('uptime').textContent = fmtUptime(s.uptime_sec);
    document.getElementById('totalInputs').textContent = s.total_inputs;
    const mgba = document.getElementById('mgbaState');
    mgba.textContent = s.mgba_connected ? 'Connected' : 'Disconnected';
    mgba.className = 'tile-value ' + (s.mgba_connected ? 'ok' : 'bad');
    document.getElementById('idleActivations').textContent = s.auto_idle_activation_count;

    const entries = Object.entries(s.inputs_by_button).sort((a, b) => b[1] - a[1]);
    const maxCount = entries.length ? entries[0][1] : 1;
    document.getElementById('buttonBreakdown').innerHTML = entries.length
      ? entries.map(([b, c]) => '<div class="bbar-row"><span class="bbar-label">' + b + '</span>'
          + '<div class="bbar-track"><div class="bbar-fill" style="width:' + Math.round(c / maxCount * 100) + '%"></div></div>'
          + '<span class="bbar-count">' + c + '</span></div>').join('')
      : '<span class="muted">no inputs fired yet</span>';

    const events = [];
    (s.error_log || []).forEach(e => events.push({
      time: e.time, tag: e.level === 'ERROR' ? 'error' : 'warning', msg: e.message,
    }));
    (s.rotation_log || []).forEach(rt => events.push({
      time: rt.time, tag: 'rotation',
      msg: rt.button + ' ' + fmtZone(rt.zone) + ': ' + (rt.old_centroid ? rt.old_centroid.join(',') : '-')
        + ' → ' + (rt.new_centroid ? rt.new_centroid.join(',') : '-'),
    }));
    events.sort((a, b) => b.time.localeCompare(a.time));
    document.getElementById('eventsLog').innerHTML = events.length
      ? events.slice(0, 15).map(e => '<div class="list-row"><span class="list-time">' + e.time + '</span>'
          + '<span class="tag ' + e.tag + '">' + e.tag.toUpperCase() + '</span>'
          + '<span class="list-msg">' + e.msg + '</span></div>').join('')
      : '<div class="muted">no events yet</div>';

    document.getElementById('inputsLog').innerHTML = s.input_log.length
      ? s.input_log.map(e => '<div class="list-row"><span class="list-time">' + e.time + '</span>'
          + '<span class="tag ' + e.source + '">' + e.button + '</span>'
          + '<span class="list-msg">' + fmtZone(e.zone) + '</span></div>').join('')
      : '<div class="muted">no inputs fired yet</div>';
  } catch (e) { /* transient poll failure, ignore */ }
}
setInterval(poll, 1000);
poll();
</script>
</body>
</html>
"""


def create_app(shared_state: SharedState) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(
            PRIMARY_TEMPLATE,
            rows=config.GRID_ROWS,
            cols=config.GRID_COLS,
            cells=_grid_cells(),
        )

    @app.route("/overlay")
    def overlay():
        roi_left = (1 - config.ROI_WIDTH_FRACTION) / 2 * 100
        roi_top = (1 - config.ROI_HEIGHT_FRACTION) / 2 * 100
        return render_template_string(
            OVERLAY_TEMPLATE,
            cells=_grid_cells(),
            roi_left=round(roi_left, 3),
            roi_top=round(roi_top, 3),
            roi_width=round(config.ROI_WIDTH_FRACTION * 100, 3),
            roi_height=round(config.ROI_HEIGHT_FRACTION * 100, 3),
        )

    @app.route("/status")
    def status_page():
        return render_template_string(
            STATUS_TEMPLATE, rotation_threshold=config.REPEAT_INPUT_ROTATION_THRESHOLD,
        )

    @app.route("/status.json")
    def status_json():
        return jsonify(shared_state.snapshot())

    @app.route("/video_feed_roi")
    def video_feed_roi():
        return Response(
            _mjpeg_generator(shared_state.get_frame_roi_bytes, shared_state.get_frame_roi_seq),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @app.route("/video_feed_full")
    def video_feed_full():
        return Response(
            _mjpeg_generator(shared_state.get_frame_full_bytes),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    return app


def _mjpeg_generator(get_bytes_fn, get_seq_fn=None):
    boundary = b"--frame\r\n"
    last_seq = None
    parts_sent = 0
    new_frames = 0
    window_start = time.monotonic()
    while True:
        frame_bytes = get_bytes_fn()
        if frame_bytes is not None:
            yield (
                boundary
                + b"Content-Type: image/jpeg\r\n\r\n"
                + frame_bytes
                + b"\r\n"
            )
            parts_sent += 1
            if get_seq_fn is not None:
                seq = get_seq_fn()
                if seq != last_seq:
                    new_frames += 1
                    last_seq = seq

        if get_seq_fn is not None:
            elapsed = time.monotonic() - window_start
            if elapsed >= 5.0:
                log.info(
                    "video_feed_roi: %.1f parts/sec sent, %.1f genuinely-new frames/sec "
                    "(over %.1fs)",
                    parts_sent / elapsed, new_frames / elapsed, elapsed,
                )
                parts_sent = 0
                new_frames = 0
                window_start = time.monotonic()

        time.sleep(1 / 30)


def run_dashboard(shared_state: SharedState):
    app = create_app(shared_state)
    log.info("Dashboard starting on http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        threaded=True,
        debug=False,
        use_reloader=False,
    )
