# Jellyfish Plays Pokemon FireRed

A livestream of jellyfish at the Monterey Bay Aquarium controls Pokemon
FireRed running in mGBA. Python watches the YouTube livestream, tracks a
jellyfish bell with computer vision, maps its position to a 3x3 grid of GBA
buttons, and sends button commands over a local TCP socket to a Lua script
running inside mGBA.

## Architecture

```
YouTube livestream --> yt-dlp/OpenCV capture --> background subtraction
  + HSV color mask --> contour scoring --> single-target tracking
  --> 3x3 zone mapping --> debounce/cooldown --> TCP --> mGBA Lua bridge
  --> emu:addKey()/emu:clearKey() --> Pokemon FireRed
```

Three pieces:
1. **Python** (`main.py` + friends) - stream capture, CV tracking, zone
   decisions, TCP server, and the dashboard (Flask, serving `/`, `/overlay`,
   and `/status`).
2. **mGBA Lua script** (`mgba_scripts/bridge.lua`) - thin TCP client running
   inside mGBA that applies received button commands.
3. **`overlay_app/`** (Electron, optional but recommended for actually
   watching the stream) - see "Live overlay & stats window" below. This is
   a separate visual layer with no bearing on tracking/input decisions;
   Python's own capture pipeline (above) is what drives the game
   regardless of whether this is running.

Note the split: the Python pipeline pulls its own copy of the stream
directly from the network (for CV tracking, deliberately paced slower than
real-time to survive this stream's frequent stalls -- see
`config.PLAYBACK_INTERVAL_SEC`), which is *not* the same video a human
would want to watch. `overlay_app/` exists so you can watch the stream at
full native smoothness in a real browser tab while a transparent overlay
tracks it independently.

## Setup

### 1. Install dependencies

```
python -m venv venv
venv\Scripts\activate        # Windows
# or: source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 2. Install yt-dlp

`yt-dlp` is installed via `requirements.txt` as a Python package, which also
gives you the `yt-dlp` command-line tool inside the venv. Verify it works:

```
yt-dlp --version
```

If you'd rather use a standalone binary instead of the pip package, see
https://github.com/yt-dlp/yt-dlp#installation and make sure `yt-dlp` is on
your `PATH`.

Note: this stream (like most YouTube live streams) only offers separate
video-only and audio-only HLS renditions -- there's no single pre-merged
"best" format. `stream_capture.py` selects video-only capped at 720p via
`config.STREAM_FORMAT_SELECTOR` (audio isn't needed for tracking). This was
confirmed working against the live stream; if YouTube changes what formats
it offers, `yt-dlp -F <url>` lists what's actually available and
`STREAM_FORMAT_SELECTOR` in `config.py` is where to adjust the selector.

### 3. Get Pokemon FireRed running in mGBA

- Install mGBA: https://mgba.io/downloads.html
- Load your own **legally-owned** Pokemon FireRed ROM file into mGBA
  (File -> Load ROM...). This project does not include, download, or source
  a ROM -- you must supply your own.

### 4. Load the Lua bridge script

- In mGBA: **Tools -> Scripting...**
- Click **Load script**, select `mgba_scripts/bridge.lua`.
- The script will log `bridge.lua loaded, will connect to 127.0.0.1:5555`
  in the scripting console and attempt to connect immediately (it will keep
  retrying every ~2 seconds until the Python server is up).
- Order doesn't matter much: you can load the Lua script before or after
  starting `main.py`. The Lua side reconnects automatically if the Python
  server isn't up yet or restarts later.

### 5. Run the tracker

```
python main.py
```

This starts:
- The stream capture thread (resolves the YouTube stream via yt-dlp, opens
  it with OpenCV, auto-reconnects on stale/failed frames).
- The tracking + zone-decision loop.
- The TCP command server on `127.0.0.1:5555`.
- The dashboard at `http://localhost:8000`.

Stop with **Ctrl+C** - this closes the TCP server, releases the video
capture, and stops all threads cleanly.

### 6. Open the dashboard

There are three pages, all sharing the same underlying tracking state:

- **`http://localhost:8000`** - the original all-in-one stream overlay: the
  live *internally-captured* video (the same throttled feed the CV
  pipeline uses, not full native fps) with a 3x3 button grid composited on
  top, styled after the classic "Fish Plays Pokemon" look. Still useful as
  an OBS **Browser Source** (default size 1280x720, configurable in
  `config.py`) if you specifically want the tracking feed baked into a
  stream output. For your own local viewing, prefer `overlay_app/` below
  instead -- it decouples the video (full native fps, a real YouTube tab)
  from the overlay, whereas this page is capped at the CV pipeline's frame
  rate (~2fps against this particular stream, see `PLAYBACK_INTERVAL_SEC`).
- **`http://localhost:8000/overlay`** - a transparent-background version of
  the same grid/HUD, meant to be embedded in a real browser window or
  composited by something else rather than viewed directly as a full page
  (no video `<img>` on this one). This is what `overlay_app/` loads into
  its transparent Electron window.
- **`http://localhost:8000/status`** - operational details: exact
  centroid/zone/debounce state, connection health (stream + mGBA), recent
  errors + forced-rotation events, session stats (uptime, total inputs,
  per-button breakdown, auto-idle activation count), and the scrolling
  input log. `overlay_app/` also opens this in its own dedicated window.

## Live overlay & stats window (optional, recommended)

`overlay_app/` is a small Electron app that gives you a much better local
viewing setup than OBS-browser-sourcing `/` directly:

1. It opens the actual YouTube stream in a normal, full-fps browser tab
   (a dedicated Chrome/Edge profile, isolated from your everyday browsing) --
   real native playback smoothness, untouched by the CV pipeline's frame
   pacing.
2. It opens a transparent, frameless, click-through, always-on-top window
   showing `/overlay` (the grid + HUD), and keeps it glued exactly to the
   video tab's on-screen position via the Chrome DevTools Protocol -- it
   tracks the actual `<video>` element's rect, not just the window bounds,
   so it stays aligned even as the tab's layout changes (theater mode,
   resizing, etc). It automatically hides when the video tab isn't the
   focused window, and automatically relaunches the video tab if you
   accidentally close it entirely.
3. It opens `/status` in its own normal, resizable window ("Jellyfish
   Stats") docked to a corner.

Setup (one-time):

```
cd overlay_app
npm install
```

To run (with `main.py` already running):

```
cd overlay_app
npm start
```

This is Windows-only (it relies on Win32 calls to track window position --
see `overlay_app/watch_window.ps1`) and requires Chrome or Edge installed
in a standard location.

## Multi-monitor note

The jellyfish stream is expected to be fullscreened on one monitor with
mGBA on a separate screen (e.g., a laptop display). Because the Python side
pulls the stream directly from its network URL via `yt-dlp`/OpenCV rather
than capturing your screen, physical window/monitor arrangement doesn't
affect the *capture/tracking* pipeline at all -- no window-selection or
screen-capture setup is needed there. `overlay_app/`'s windows (if you use
it) do care about arrangement, but position themselves automatically: the
transparent overlay always follows wherever the video tab actually is, on
whichever monitor.

## Tuning CV thresholds

All tunables live in `config.py`. If tracking looks unreliable once the
stream is actually running, the most useful knobs, roughly in the order
you'll want to touch them:

1. **ROI crop** (`ROI_WIDTH_FRACTION`, `ROI_HEIGHT_FRACTION`) - watch the
   `/status` page's "full frame with ROI boundary" thumbnail and adjust so
   it excludes the "Monterey Bay Aquarium" watermark (top-right) while
   still covering the area where jellyfish are consistently in frame.
2. **HSV color range** (`BELL_HSV_LOWER` / `BELL_HSV_UPPER`, and the
   `_2` wraparound pair) - color-based segmentation is the *primary*
   detector (see point 3), so this matters more than it might look. If
   bells aren't being detected, or the mask is picking up
   background/tentacles, sample actual pixel colors from a screenshot of
   the stream and widen or shift these ranges.
3. **`MORPH_OPEN_KERNEL_SIZE` / `MORPH_OPEN_ITERATIONS`** (default 17px /
   3 iterations) - this is the most impactful knob in practice. It's
   applied to the color mask to sever thin tentacle strands from the round
   bell core before contour extraction; too small and a bell's contour
   gets fused with its own (and neighboring jellies') tentacle fringe,
   which tanks circularity and the bell never qualifies even though it's
   clearly visible. Raise it if bells still aren't isolating cleanly at
   high zoom/resolution; lower it (a real bell candidate needs to survive
   the erosion) if a genuinely large, close bell is getting eroded away to
   nothing -- confirmed live that too large a value (the original default
   here was 25px) can erode away legitimate bells entirely on some frames,
   not just tentacles, producing zero candidates despite several clearly
   visible bells.
   - Note: background subtraction (`MOG2_VAR_THRESHOLD`, `MOG2_HISTORY`)
     is *not* a hard requirement for detection -- it only contributes a
     motion-overlap scoring bonus (`SCORE_WEIGHT_MOTION`). An earlier
     version required motion AND color as a hard gate, but MOG2 absorbs a
     slow-moving or momentarily stationary bell into its background model
     within seconds, so that gate caused real, clearly-visible bells to
     never qualify. Confirmed against live footage before fixing: 40/41
     consecutive frames produced zero qualifying candidates despite
     several obvious bells in frame.
4. **Circularity / solidity thresholds** (`MIN_CIRCULARITY`,
   `MIN_SOLIDITY`) - lower these if a clearly face-on bell is being
   rejected; raise them if side-profile jellyfish or tentacle clumps are
   getting through. In testing, `MIN_SOLIDITY` rarely ends up being the
   filter that rejects a candidate (circularity usually does first) -- if
   you're chasing false positives, check circularity first.
5. **`MIN_CONTOUR_AREA`** - raise if small noise/tentacle fragments pass
   the shape filters; lower if a genuine but distant/small bell is being
   rejected.
6. **Debounce / cooldown** (`DEBOUNCE_FRAMES`, `INPUT_COOLDOWN_SEC`) -
   raise `DEBOUNCE_FRAMES` if inputs fire too eagerly near zone boundaries;
   the cooldown defaults to the confirmed 0.1s minimum and generally
   shouldn't need to go lower.
7. **Grid -> button mapping** (`GRID_BUTTON_MAP`) - if START/SELECT (the
   corner zones) prove hard for a jellyfish to reliably hit, just remap
   which zone maps to which button; the whole grid is one config dict, and
   the overlay's icons/badges follow the mapping automatically.
8. **Non-uniform zone sizing** (`GRID_COL_FRACTIONS`,
   `GRID_ROW_FRACTIONS_BY_COL`) - the 3x3 grid isn't cosmetic equal thirds;
   each button's actual "hit area" is independently sized, since a bigger
   zone gets landed in (and fires) more often. Re-tune this from
   `/status`'s live "inputs by button" breakdown: divide each button's
   observed count by its current zone area to see roughly how
   "attractive" that part of the tank is per unit area (some regions get
   visited far more than others, independent of zone size), then resize
   zones so the buttons you care about get closer to the firing rate you
   want. Both fraction lists tile the ROI exactly (column widths sum to
   1.0; within each column, row heights sum to 1.0), so any change to one
   zone's size necessarily affects its neighbors in the same
   row/column -- expect to touch several values together, not just one.
9. **Repeat-input rotation** (`REPEAT_INPUT_ROTATION_THRESHOLD`,
   `FORCED_ROTATION_MIN_DISTANCE`) - if one still, camera-facing jellyfish
   locks in and fires the same button over and over, `tracker.force_rotate()`
   forces a switch to a different qualifying candidate after N consecutive
   identical inputs (default 7). It requires the new candidate to be both
   far enough away (`FORCED_ROTATION_MIN_DISTANCE`, default 150px) *and* in
   a genuinely different zone than the one that triggered the rotation --
   distance alone isn't enough, since zones are non-uniformly sized (see
   point 8) and a different physical jellyfish can still sit in the same
   large zone. Falls through gracefully (keeps the current lock, retries
   next fire) if no such candidate exists that frame. Visible on `/status`
   as a distinct "Forced rotations" event, separate from normal inputs.
10. **Candidate scoring weights** (`SCORE_WEIGHT_CIRCULARITY`,
    `SCORE_WEIGHT_SOLIDITY`, `SCORE_WEIGHT_AREA`, `SCORE_WEIGHT_MOTION`,
    `SCORE_AREA_NORMALIZER_FRACTION`) - when multiple bells qualify in the
    same frame, this decides which one gets locked onto. Shape
    (circularity + solidity) should generally dominate so a large-but-messy
    blob can't outrank a cleaner, rounder one; area is scored as a genuine
    proportion of the ROI (`SCORE_AREA_NORMALIZER_FRACTION`, default 0.05 --
    a bell covering >=5% of the frame gets full area credit) so it's not a
    stale absolute pixel count that goes wrong if `ROI_WIDTH_FRACTION`/
    `ROI_HEIGHT_FRACTION` change. Raise `SCORE_WEIGHT_AREA` to more
    consistently prefer the closest/most prominent bell (also generally
    easier to hold a stable lock on); raise
    `SCORE_WEIGHT_CIRCULARITY`/`SCORE_WEIGHT_SOLIDITY` if a large blob that
    isn't a clean bell shape is winning over a smaller, rounder one.

To see exactly why a frame isn't producing a detection, set
`LOG_LEVEL = "DEBUG"` in `config.py` and watch the console: every frame
logs a line like `detect_candidates: contours=94 area_reject=81
circularity_reject=13 solidity_reject=0 qualified=0`, showing exactly which
filter is rejecting candidates.

## Troubleshooting

**mGBA console only shows "bridge.lua loaded...", never "connected"**
Reconnect attempts happen on the emulator's `frame` callback, so they only
run while a ROM is loaded and unpaused. Make sure `python main.py` is
already running (its terminal should show `TCP command server listening on
127.0.0.1:5555`) and the emulator is unpaused, then reload `bridge.lua`
(Tools -> Scripting... -> Load script). You should see `attempting
connection to 127.0.0.1:5555 ...` followed by either `connected to
127.0.0.1:5555` or a specific `connection attempt failed: <reason>` message
-- the latter tells you exactly what's wrong (e.g. connection refused means
the Python server isn't up yet).

**Inputs aren't firing / nothing detected**
Check `/status` for `stream_state` (should be `connected`), then set
`LOG_LEVEL = "DEBUG"` in `config.py` to see per-filter detection stats in
the console (see the CV tuning section above).

## Design decisions (see `jellyfish-plays-pokemon-prompt.md` for full spec)

- Fully autonomous: no manual override/hotkey path exists to inject human
  input. If the game gets stuck in a way the auto-idle fallback can't
  resolve, that's handled by manually restarting/reloading outside the
  running system.
- Auto-idle fallback (fires when no jellyfish has qualified for an extended
  period, default 45s, then every 7s) presses **A only**.
- 0.1s minimum cooldown between fired inputs.
- An input only ever fires off a candidate actually matched *that exact
  frame* -- `tracker.state.locked` alone isn't a tight enough gate, since
  it deliberately stays true for a grace period (`TRACK_MAX_LOST_FRAMES`)
  after the last real detection, using a frozen position for the debug
  overlay's dot. Feeding that stale position into the input decision too
  would fire on wherever a jellyfish *used to be* once it's moved on.
- One jellyfish can't monopolize every input forever: after
  `REPEAT_INPUT_ROTATION_THRESHOLD` consecutive identical inputs, the
  tracker is forced to rotate its lock onto a different, currently-visible
  candidate in a genuinely different zone (see tuning point 9 above).

## Project structure

```
jellyfish-plays-pokemon/
├── README.md
├── config.py            # all tunable constants
├── stream_capture.py    # yt-dlp resolution + threaded OpenCV capture with reconnect
├── tracker.py           # background subtraction, color mask, contour scoring, tracking
├── zone_mapper.py        # grid mapping, debounce, rate limiting, auto-idle
├── tcp_server.py         # Python-side TCP server
├── dashboard.py          # Flask dashboard: "/", "/overlay", "/status"
├── main.py               # wires everything together
├── mgba_scripts/
│   └── bridge.lua        # Lua script loaded into mGBA's Scripting console
├── overlay_app/          # optional Electron app -- see "Live overlay & stats window"
│   ├── main.js            # launches + positions the video tab, overlay, stats windows
│   ├── watch_window.ps1   # Win32 window-position/visibility polling
│   └── package.json
├── launch_overlay.ps1    # legacy: opens "/" as a chromeless popup (see overlay_app/ instead)
└── requirements.txt
```

## Stretch goals (not yet implemented)

- Multi-jellyfish "majority vote" mode (`config.TRACKING_MODE = "swarm"` is
  reserved for this; `tracker.detect_candidates()` already returns *all*
  qualifying candidates each frame, so a swarm mode can reuse it directly
  instead of just the single best/nearest match).
- Twitch chat integration as a fallback/override input source.
- Smarter re-acquisition heuristics to avoid ping-ponging between two
  jellyfish near a zone boundary.
- Save/replay of tracking + input logs.

## Out of scope

- This project does not source, download, or embed a Pokemon FireRed ROM.
- This project does not re-host, download, or redistribute the aquarium's
  video stream; the stream is only pulled in real time for local tracking.
