"""
Central configuration for Jellyfish Plays Pokemon.

Every tunable knob lives here. Nothing below should need code changes to
adjust behavior -- just edit values and restart main.py.
"""

# --------------------------------------------------------------------------
# Stream ingestion
# --------------------------------------------------------------------------

STREAM_URL = "https://www.youtube.com/watch?v=eQ_foBERmzA"

# yt-dlp format selector. This stream (like most YouTube live streams)
# offers no pre-merged "best" format -- only separate video-only and
# audio-only HLS renditions (confirmed via `yt-dlp -F`). We don't need
# audio for CV tracking, so select video-only capped at 720p: plenty of
# resolution for jellyfish tracking, far lighter on bandwidth/CPU than the
# available 1080p rendition.
STREAM_FORMAT_SELECTOR = "bestvideo[height<=720]/best"

# How often (seconds) to check that new frames are still arriving before
# declaring the capture stale and forcing a reconnect (which re-resolves
# the yt-dlp direct URL, since those expire periodically).
STREAM_STALE_TIMEOUT_SEC = 10.0

# Max seconds to wait on a single cap.read() call before treating the
# connection as stuck and forcing a reconnect with a freshly resolved URL.
# cap.read() is a blocking call -- confirmed via live testing that it can
# itself hang for 5+ seconds waiting on network data from the specific
# resolved googlevideo URL/CDN edge, even while YouTube's own web player
# (which juggles multiple in-flight requests instead of blocking on one)
# keeps playing smoothly on the same connection. STREAM_STALE_TIMEOUT_SEC
# above can't catch this -- it only checks elapsed time *between* completed
# reads, so it never gets a chance to run while a single read() is still
# in flight. Set comfortably above normal read latency (observed up to
# ~0.5s even under load) but well below observed genuine stalls (5s+).
STREAM_READ_TIMEOUT_SEC = 3.0

# Seconds to wait between reconnect attempts if resolution/opening fails.
# Kept short: confirmed via live testing that reconnects triggered by
# STREAM_READ_TIMEOUT_SEC (a stuck read, not a hard failure) happen fairly
# often (every 10-20s against this stream) and a fresh URL typically
# recovers immediately, so a long cautious backoff here mostly just adds
# dead time -- it was the single biggest source of "0 new frames" windows
# observed once the read-timeout watchdog was in place.
STREAM_RECONNECT_BACKOFF_SEC = 1.5

# Elastic playback buffer: frames are released to the processing loop at a
# steady PLAYBACK_INTERVAL_SEC pace, drawn from a backlog that's allowed to
# build up to PLAYBACK_BUFFER_SEC seconds deep, instead of handing over
# whatever's freshest immediately. This is the same trick a real video
# player uses to stay smooth through a brief upstream hiccup: it isn't
# enough to just delay everything by a fixed amount (that only shifts a
# gap in time, it doesn't shrink it -- confirmed this the hard way with an
# earlier fixed-delay-only implementation that measurably didn't help).
# What actually works is under-consuming the average arrival rate so a
# cushion accumulates during good periods, which then gets drawn down
# during a stall instead of the pipeline stalling right along with it.
#
# Confirmed via live testing that this stream stalls roughly every 7-9s
# for up to ~5-6s at a time even with the STREAM_READ_TIMEOUT_SEC watchdog
# (see above) reconnecting as fast as possible -- lowering stream
# resolution and disabling all client-side buffering/low-latency settings
# were both tested live and neither changed that pattern at all, so it's
# an upstream/CDN characteristic of this specific stream, not something
# fixable by tuning capture settings; this buffer is what actually hides
# it from the viewer.
#
# PLAYBACK_BUFFER_SEC: how deep a backlog is allowed to accumulate (also
# how long playback initially waits before starting, to build an initial
# cushion). Raise for more resilience against back-to-back stalls, at the
# cost of more startup delay, more latency between a real tank event and
# it appearing on screen (jellyfish decisions/inputs lag behind the live
# tank by roughly this much too), and a bit more memory (each buffered
# second is roughly one frame at the stream's actual arrival rate, a few
# MB each).
PLAYBACK_BUFFER_SEC = 12.0

# PLAYBACK_INTERVAL_SEC: target seconds between released frames (i.e. 1 /
# target fps). Must be set comfortably ABOVE 1/(true sustained average
# arrival rate) or the backlog structurally drains over time no matter how
# deep PLAYBACK_BUFFER_SEC is -- live testing measured a genuinely-new-
# frames/sec average of roughly 2.4-4.6 for this stream (bursty: brief
# spikes to 7-10 right after a reconnect, otherwise lower), so 0.5s (2fps)
# leaves a deliberately generous safety margin below that. Lower for more
# responsiveness if live testing shows the buffer comfortably staying
# full; the video's own /status.json fps figure reports the achieved
# rate, and STREAM_STALE_TIMEOUT_SEC-style monitoring of buffer depth
# would be the thing to watch if tightening this.
PLAYBACK_INTERVAL_SEC = 0.5


# --------------------------------------------------------------------------
# Region of interest (ROI) cropping
# --------------------------------------------------------------------------

# Fractions of full-frame width/height to keep, centered. E.g. 0.65 keeps
# the middle 65% along that axis and crops 17.5% off each side.
#
# Confirmed via a captured full frame (see /video_feed_full) that the only
# thing outside a tight center crop is the "Monterey Bay Aquarium" watermark
# in the top-right corner -- and that's white/desaturated, so it can't match
# BELL_HSV_LOWER/UPPER (which requires the warm-hued, saturated bell color)
# regardless of ROI size. Widened close to the full frame so more of the
# tank (and the visible/overlay grid) is covered instead of a small centered
# crop, without risking watermark false positives.
ROI_WIDTH_FRACTION = 0.95
ROI_HEIGHT_FRACTION = 0.95


# --------------------------------------------------------------------------
# Background subtraction / motion mask
# --------------------------------------------------------------------------

MOG2_HISTORY = 500
MOG2_VAR_THRESHOLD = 16
MOG2_DETECT_SHADOWS = False

# Morphological opening kernel size (pixels) applied to the color mask to
# sever thin tentacle strands from the round bell core before contour
# extraction. Needs to be large enough to fully erase tentacles (a few px
# wide) while leaving the bell body (tens of px across) intact -- 5px was
# too small in practice (left tentacle-fringed blobs whose overall contour
# wasn't circular even though the bell within it was).
#
# 25px/3 iterations (the original value here) turned out to be too
# aggressive: confirmed against live captured frames that it fully erodes
# away legitimate bells whenever they're a bit smaller/farther/angled, not
# just the tentacles -- one real frame produced a completely empty mask
# (zero contours at all) despite four clearly visible, camera-facing bells,
# which left the tracker showing a stale last-known centroid instead of
# re-detecting. 17px/3 iterations was tested against several live frames
# (including that failing one) and cleanly isolated every visible bell dome
# as its own contour -- tentacles still fully stripped, bells no longer
# eroded away -- so it detects far more consistently without letting any
# tentacle-fringed blob back in (still rejected by MIN_CIRCULARITY/
# MIN_SOLIDITY below).
MORPH_OPEN_KERNEL_SIZE = 17
MORPH_OPEN_ITERATIONS = 3


# --------------------------------------------------------------------------
# Color mask (HSV) for the warm orange/tan/pink jellyfish bell
# --------------------------------------------------------------------------
# OpenCV HSV ranges: H in [0,179], S and V in [0,255].
# These are starting points tuned for a warm bell against deep blue water --
# expect to adjust after watching the live feed.

BELL_HSV_LOWER = (0, 40, 90)
BELL_HSV_UPPER = (30, 200, 255)

# A second wraparound range near red (H close to 179) in case the bell's
# hue sits at the top of the HSV wheel under certain lighting. Set to None
# to disable.
BELL_HSV_LOWER_2 = (170, 40, 90)
BELL_HSV_UPPER_2 = (179, 200, 255)


# --------------------------------------------------------------------------
# Contour filtering (bell-only, facing-camera-only detection)
# --------------------------------------------------------------------------

MIN_CONTOUR_AREA = 250           # px^2, in ROI-cropped coordinates
MIN_CIRCULARITY = 0.7            # 4*pi*area / perimeter^2
# Raised from 0.85: solidity (contour area / convex hull area) is the more
# direct signal against tentacle strands specifically -- they're what
# breaks convexity -- and confirmed via live captured frames that genuine
# bell domes measure 0.95-0.99 solidity even when large/prominent, so this
# still has comfortable margin without risking real detections. (Left
# MIN_CIRCULARITY alone: a confirmed-correct, large/prominent dome measured
# only 0.73 circularity in one live sample -- barely above the old floor --
# so raising that one risks rejecting exactly the big, prominent jellyfish
# SCORE_WEIGHT_AREA below is trying to prefer.)
MIN_SOLIDITY = 0.90              # contour area / convex hull area

# Weights for combining circularity + solidity + normalized area + a
# motion-overlap bonus into a single candidate score used for initial
# lock-on and re-acquisition. Motion is a soft bonus, not a hard filter --
# see detect_candidates() docstring for why an AND-gate on motion broke
# detection of real, slow-moving/stationary bells.
#
# Shape (circularity + solidity) carries the most weight again -- confirmed
# via live testing that pushing area up too far (an earlier version of this
# had AREA=0.45 vs shape=0.45 combined) risked a large, low-circularity
# blob outranking a cleaner, rounder one it shouldn't have. Area still
# carries more than its original weight (0.15) so a bigger/closer bell is
# still preferred over a smaller one when shape is comparable, and a bigger
# bell is genuinely easier to hold a stable lock on -- it just no longer
# dominates shape quality outright.
SCORE_WEIGHT_CIRCULARITY = 0.30
SCORE_WEIGHT_SOLIDITY = 0.35
SCORE_WEIGHT_AREA = 0.25
SCORE_WEIGHT_MOTION = 0.10

# Fraction of the ROI's total area a candidate needs to reach for full
# credit on the area score component -- i.e. this is a literal "proportion
# of the screen" threshold, not an absolute pixel count, so it scales
# automatically if ROI_WIDTH_FRACTION/ROI_HEIGHT_FRACTION change (this
# replaced an earlier fixed-pixel SCORE_AREA_NORMALIZER that went stale
# the moment the ROI was widened -- most qualifying bells were already
# maxing out its area score, making area barely differentiate anything).
SCORE_AREA_NORMALIZER_FRACTION = 0.05


# --------------------------------------------------------------------------
# Single-target tracking
# --------------------------------------------------------------------------

# Max centroid-to-centroid distance (px, ROI coordinates) for a candidate
# in a new frame to be considered "the same jellyfish" as last frame.
TRACK_MAX_MATCH_DISTANCE = 120

# If the tracked jellyfish has no matching candidate for this many
# consecutive frames, drop the lock and re-acquire from the best-scoring
# candidate anywhere in the ROI on the next qualifying frame.
TRACK_MAX_LOST_FRAMES = 15

# Exponential smoothing factor for the centroid (0 < alpha <= 1).
# Higher = more responsive/jittery, lower = smoother/laggier.
CENTROID_SMOOTHING_ALPHA = 0.35

# Tracking mode. "single" tracks one jellyfish. "swarm" (stretch goal) is
# reserved for a future majority-vote implementation.
TRACKING_MODE = "single"

# Minimum centroid distance (ROI px) a candidate must be from the current
# lock's centroid to be eligible during a forced rotation (see
# REPEAT_INPUT_ROTATION_THRESHOLD below) -- keeps a rotation from landing
# right back on the same jellyfish (or another contour fragment of the same
# bell). Deliberately larger than TRACK_MAX_MATCH_DISTANCE above, since that
# constant means the opposite thing (max distance to still count as the
# *same* jellyfish across frames).
FORCED_ROTATION_MIN_DISTANCE = 150


# --------------------------------------------------------------------------
# Extended no-detection fallback ("auto-idle")
# --------------------------------------------------------------------------

# If no jellyfish has qualified for this many seconds, start firing
# auto-idle presses.
AUTO_IDLE_TRIGGER_SEC = 45.0

# Interval (seconds) between auto-idle presses while in fallback mode.
AUTO_IDLE_INTERVAL_SEC = 7.0

# Auto-idle always presses this button only (design decision: A only).
AUTO_IDLE_BUTTON = "A"


# --------------------------------------------------------------------------
# Zone mapping / input decision
# --------------------------------------------------------------------------

GRID_ROWS = 3
GRID_COLS = 3

# (row, col) -> button, with (0,0) = top-left ... (2,2) = bottom-right.
GRID_BUTTON_MAP = {
    (0, 0): "B",       # top-left: cancel / hold-to-run
    (0, 1): "UP",       # top-center
    (0, 2): "START",    # top-right: main menu
    (1, 0): "LEFT",     # middle-left
    (1, 1): "A",        # center: confirm / interact
    (1, 2): "RIGHT",    # middle-right
    (2, 0): "SELECT",   # bottom-left: item register shortcut
    (2, 1): "DOWN",     # bottom-center
    (2, 2): "R",        # bottom-right: unused in FireRed, mapped for completeness
}

# Non-uniform zone sizing: each button's actual "hit area" in the ROI,
# rather than uniform equal thirds. Larger area = more likely for the
# jellyfish's centroid to land in and dwell in that zone = fired more often.
#
# Structure: column widths (GRID_COL_FRACTIONS, 3 values summing to 1.0),
# then within each column, independent row-height fractions
# (GRID_ROW_FRACTIONS_BY_COL[col], each a list of 3 values summing to 1.0).
# Row heights are independent *per column* (not shared across the whole
# row) so e.g. START's cell can shrink without also shrinking B's cell,
# even though they're both in row 0 -- this still tiles the ROI perfectly
# (no gaps/overlaps) since each column is just a vertical strip split
# independently. See zone_mapper.centroid_to_zone() for how these are used,
# and dashboard._grid_cells() for how the same fractions drive the visual
# overlay so it honestly reflects actual trigger-area size, not just a
# cosmetic tic-tac-toe grid.
#
# Re-tuned against a later, larger observed session (LEFT=131, RIGHT=100,
# DOWN=98, B=66, A=52, UP=37, R=30, START=16, SELECT=11) with two explicit
# goals this time: the four directionals should fire roughly equally often
# (UP was badly under-firing at 37 vs LEFT's 131 -- a >3x spread despite
# UP's zone not being dramatically smaller), and A should clearly lead
# every other button (it's the confirm/interact button -- the most
# important one -- but was firing *less* than B, a minor button).
#
# Method: divide each button's observed count by its old area to get a
# rough "attractiveness per unit area" for that screen region (jellyfish
# apparently favor the LEFT/DOWN regions of this tank well beyond what
# their old zone size alone explains), then size the new zones so equal
# attractiveness-adjusted area gives roughly equal (for the directionals)
# or boosted (for A) predicted counts. Blended partway toward the
# old area rather than solving for exact equality, since a single
# session's counts are noisy, especially for the low-count minor
# buttons -- re-tune again from /status's live breakdown after running a
# while, same as before.
GRID_COL_FRACTIONS = [0.23, 0.54, 0.23]  # col 0 (B/LEFT/SELECT), col 1 (UP/A/DOWN), col 2 (START/RIGHT/R)
GRID_ROW_FRACTIONS_BY_COL = [
    [0.28, 0.56, 0.16],  # col 0: B, LEFT, SELECT
    [0.37, 0.38, 0.25],  # col 1: UP, A, DOWN
    [0.10, 0.73, 0.17],  # col 2: START, RIGHT, R
]

# Number of consecutive frames the smoothed centroid must stay in the same
# zone before an input fires.
#
# 12 frames (6s at the ~2fps this stream sustains, see PLAYBACK_INTERVAL_SEC)
# turned out to routinely lose the race against this stream's reconnect
# cadence: confirmed via live logs that stream_capture reconnects roughly
# every 7-9s, and each reconnect's multi-second stall lets the camera
# pan/zoom enough that the tracked jellyfish reappears past
# TRACK_MAX_MATCH_DISTANCE away, forcing a fresh re-acquire (logged as
# "Re-acquired jellyfish") that resets this debounce hold back to zero --
# jellyfish were being detected correctly the whole time, they just kept
# getting bounced to a new lock before 12 frames could ever complete,
# producing 15-20s+ gaps with no input despite continuous, correct
# detection. Lowered to 3 (~1.5s) for faster response -- if it starts
# causing spurious inputs from single noisy/transient frames, raise this
# back up rather than trying to fix it via MIN_CIRCULARITY/MIN_SOLIDITY
# (those are about bell shape, not dwell time).
DEBOUNCE_FRAMES = 3

# Minimum seconds between fired inputs.
INPUT_COOLDOWN_SEC = 0.1

# If the same (button, zone) fires this many consecutive times -- i.e. one
# still, camera-facing jellyfish is locked on and keeps winning re-matching
# every frame -- force the tracker to drop that lock and rotate onto a
# different qualifying candidate elsewhere in the ROI, so inputs spread
# across multiple jellyfish instead of one individual monopolizing every
# press. Only counts genuinely jellyfish-driven fires (never AUTO-IDLE,
# which has no active lock to rotate away from) and only increments when an
# input actually fires (never on every frame -- debounce/cooldown above are
# unaffected). See ZoneMapper._consecutive_repeat_count / tracker.py's
# force_rotate().
REPEAT_INPUT_ROTATION_THRESHOLD = 7


# --------------------------------------------------------------------------
# TCP bridge (Python server <-> mGBA Lua client)
# --------------------------------------------------------------------------

TCP_HOST = "127.0.0.1"
TCP_PORT = 5555


# --------------------------------------------------------------------------
# mGBA Lua bridge (informational; actual values live in bridge.lua since
# that side isn't fed from this Python config -- keep values in sync)
# --------------------------------------------------------------------------

LUA_KEY_HOLD_FRAMES = 6   # how many emulated frames to hold a button


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8000
DASHBOARD_WIDTH = 1280
DASHBOARD_HEIGHT = 720
DASHBOARD_LOG_LENGTH = 10
DASHBOARD_MJPEG_QUALITY = 80   # JPEG quality 0-100 for the MJPEG stream


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
