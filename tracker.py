"""
Jellyfish bell tracking: ROI cropping, background subtraction + HSV color
masking to isolate bell-facing-camera candidates, contour scoring, and
single-target tracking with nearest-centroid matching and re-acquisition.

Structured so a future "swarm" mode (score/track all qualifying candidates
instead of just one) can reuse detect_candidates() without changes.
"""

import logging
import math
from dataclasses import dataclass

import cv2
import numpy as np

import config

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    contour: np.ndarray
    centroid: tuple  # (x, y) in ROI coordinates
    area: float
    circularity: float
    solidity: float
    score: float


@dataclass
class DetectionStats:
    """Per-frame filter pass/fail counts, for debugging why detection is
    (or isn't) firing -- see which specific filter is rejecting contours."""
    contours_found: int = 0
    area_reject: int = 0
    circularity_reject: int = 0
    solidity_reject: int = 0
    qualified: int = 0

    def __str__(self):
        return (
            f"contours={self.contours_found} "
            f"area_reject={self.area_reject} "
            f"circularity_reject={self.circularity_reject} "
            f"solidity_reject={self.solidity_reject} "
            f"qualified={self.qualified}"
        )


@dataclass
class TrackState:
    locked: bool = False
    centroid: tuple | None = None          # smoothed centroid, ROI coords
    raw_centroid: tuple | None = None       # last raw (unsmoothed) centroid
    lost_frames: int = 0
    last_candidate: Candidate | None = None
    last_qualifying_time: float | None = None
    consecutive_no_detection_frames: int = 0


def compute_roi(frame_shape):
    """Return (x0, y0, x1, y1) pixel bounds of the centered ROI crop."""
    h, w = frame_shape[:2]
    roi_w = int(w * config.ROI_WIDTH_FRACTION)
    roi_h = int(h * config.ROI_HEIGHT_FRACTION)
    x0 = (w - roi_w) // 2
    y0 = (h - roi_h) // 2
    return x0, y0, x0 + roi_w, y0 + roi_h


def _circularity(area: float, perimeter: float) -> float:
    if perimeter <= 0:
        return 0.0
    return float(4 * math.pi * area / (perimeter * perimeter))


def _solidity(contour, area: float) -> float:
    hull = cv2.convexHull(contour)
    hull_area = cv2.contourArea(hull)
    if hull_area <= 0:
        return 0.0
    return float(area / hull_area)


def _color_mask(hsv_roi: np.ndarray) -> np.ndarray:
    mask = cv2.inRange(hsv_roi, config.BELL_HSV_LOWER, config.BELL_HSV_UPPER)
    if config.BELL_HSV_LOWER_2 is not None and config.BELL_HSV_UPPER_2 is not None:
        mask2 = cv2.inRange(hsv_roi, config.BELL_HSV_LOWER_2, config.BELL_HSV_UPPER_2)
        mask = cv2.bitwise_or(mask, mask2)
    return mask


class JellyfishTracker:
    def __init__(self):
        self._bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=config.MOG2_HISTORY,
            varThreshold=config.MOG2_VAR_THRESHOLD,
            detectShadows=config.MOG2_DETECT_SHADOWS,
        )
        # Large kernel: color-mask segmentation is the primary detector (see
        # detect_candidates), and this needs to be big enough to fully erase
        # thin tentacle strands (a few px wide) while leaving the round bell
        # core (tens of px across) intact -- confirmed empirically against
        # live footage; a small kernel (~5px) leaves tentacle-fringed blobs
        # whose overall contour is not circular even though the bell within
        # it is.
        self._morph_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (config.MORPH_OPEN_KERNEL_SIZE, config.MORPH_OPEN_KERNEL_SIZE),
        )
        self._motion_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.state = TrackState()
        # Candidates from the most recent update() call, kept around so
        # force_rotate() can pick a new lock from the same frame's
        # detections instead of needing to wait for the next one.
        self._last_candidates: list[Candidate] = []

    def detect_candidates(self, roi_bgr: np.ndarray):
        """Run color-based segmentation (primary) + shape filtering to find
        qualifying bell candidates; motion is used only as a soft scoring
        bonus, not a hard gate.

        Background subtraction was originally ANDed with the color mask as
        a hard requirement, but MOG2 absorbs a slow-moving or momentarily
        stationary bell into its background model within a few seconds --
        their bulk body then produces near-zero motion signal even though
        they're clearly visible and alive, so requiring motion caused real,
        obviously-camera-facing bells to never qualify (confirmed against
        live footage: 40/41 consecutive frames produced zero qualifying
        candidates despite several clear bells in frame; the one frame that
        did qualify was a small tentacle-fragment false positive, not a
        bell). Motion is still useful as a secondary signal -- a genuinely
        alive, pulsing jellyfish tends to show at least some recent motion
        somewhere in its body -- so it contributes a scoring bonus instead.
        """
        motion_mask = self._bg_subtractor.apply(roi_bgr)
        # Treat only "definitely foreground" pixels as motion (MOG2 shadow
        # value is 127 when detectShadows=True; we disable it by default,
        # but threshold defensively regardless).
        _, motion_mask = cv2.threshold(motion_mask, 200, 255, cv2.THRESH_BINARY)
        motion_mask = cv2.morphologyEx(
            motion_mask, cv2.MORPH_OPEN, self._motion_kernel, iterations=1,
        )

        hsv_roi = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        color_mask = _color_mask(hsv_roi)
        cleaned = cv2.morphologyEx(
            color_mask, cv2.MORPH_OPEN, self._morph_kernel,
            iterations=config.MORPH_OPEN_ITERATIONS,
        )

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        roi_area = roi_bgr.shape[0] * roi_bgr.shape[1]
        area_normalizer = roi_area * config.SCORE_AREA_NORMALIZER_FRACTION

        stats = DetectionStats(contours_found=len(contours))
        candidates = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < config.MIN_CONTOUR_AREA:
                stats.area_reject += 1
                continue
            perimeter = cv2.arcLength(contour, True)
            circularity = _circularity(area, perimeter)
            if circularity < config.MIN_CIRCULARITY:
                stats.circularity_reject += 1
                continue
            solidity = _solidity(contour, area)
            if solidity < config.MIN_SOLIDITY:
                stats.solidity_reject += 1
                continue

            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                continue
            cx = moments["m10"] / moments["m00"]
            cy = moments["m01"] / moments["m00"]

            mask_fill = np.zeros(cleaned.shape, dtype=np.uint8)
            cv2.drawContours(mask_fill, [contour], -1, 255, -1)
            motion_overlap_px = cv2.countNonZero(cv2.bitwise_and(mask_fill, motion_mask))
            motion_fraction = min(1.0, motion_overlap_px / area) if area > 0 else 0.0

            # Proportion of the whole ROI this candidate covers, capped at 1.0
            # once it reaches SCORE_AREA_NORMALIZER_FRACTION -- see config.py.
            area_score = min(1.0, area / area_normalizer)
            score = (
                config.SCORE_WEIGHT_CIRCULARITY * circularity
                + config.SCORE_WEIGHT_SOLIDITY * solidity
                + config.SCORE_WEIGHT_AREA * area_score
                + config.SCORE_WEIGHT_MOTION * motion_fraction
            )

            candidates.append(Candidate(
                contour=contour,
                centroid=(cx, cy),
                area=area,
                circularity=circularity,
                solidity=solidity,
                score=score,
            ))

        stats.qualified = len(candidates)
        log.debug("detect_candidates: %s", stats)

        return candidates, cleaned, stats

    def update(self, roi_bgr: np.ndarray, now: float):
        """
        Process one frame. Returns (smoothed_centroid_or_None, candidate_or_None,
        debug_mask, stats). Updates self.state in place.
        """
        candidates, debug_mask, stats = self.detect_candidates(roi_bgr)
        self._last_candidates = candidates
        state = self.state

        chosen = None

        if candidates:
            if state.locked and state.raw_centroid is not None:
                chosen = self._match_nearest(candidates, state.raw_centroid)

            if chosen is None:
                # Either not locked yet, or lost track -> re-acquire best score.
                chosen = max(candidates, key=lambda c: c.score)
                if not state.locked:
                    log.info("Locked onto jellyfish at %.0f,%.0f (score=%.2f)",
                              chosen.centroid[0], chosen.centroid[1], chosen.score)
                else:
                    log.info("Re-acquired jellyfish at %.0f,%.0f (score=%.2f)",
                              chosen.centroid[0], chosen.centroid[1], chosen.score)
                state.locked = True
                state.lost_frames = 0

        if chosen is not None:
            state.raw_centroid = chosen.centroid
            state.last_candidate = chosen
            state.lost_frames = 0
            state.last_qualifying_time = now
            state.consecutive_no_detection_frames = 0

            if state.centroid is None:
                state.centroid = chosen.centroid
            else:
                a = config.CENTROID_SMOOTHING_ALPHA
                px, py = state.centroid
                nx, ny = chosen.centroid
                state.centroid = (a * nx + (1 - a) * px, a * ny + (1 - a) * py)
        else:
            state.consecutive_no_detection_frames += 1
            if state.locked:
                state.lost_frames += 1
                if state.lost_frames > config.TRACK_MAX_LOST_FRAMES:
                    log.info("Lost jellyfish track after %d frames without a match",
                              state.lost_frames)
                    state.locked = False
                    state.raw_centroid = None
                    state.last_candidate = None
                    # Keep state.centroid as last-known position (hold last
                    # input state per Component 2 spec) until a new lock.

        return state.centroid, chosen, debug_mask, stats

    @staticmethod
    def _match_nearest(candidates, prev_centroid):
        px, py = prev_centroid
        best = None
        best_dist = None
        for c in candidates:
            cx, cy = c.centroid
            dist = math.hypot(cx - px, cy - py)
            if dist > config.TRACK_MAX_MATCH_DISTANCE:
                continue
            if best_dist is None or dist < best_dist:
                best = c
                best_dist = dist
        return best

    def force_rotate(self, now: float):
        """Force-drop the current lock and re-acquire a different
        qualifying candidate from the most recently detected frame,
        excluding anything within config.FORCED_ROTATION_MIN_DISTANCE of
        the current lock's centroid. Called by main.py when the same
        input has fired config.REPEAT_INPUT_ROTATION_THRESHOLD times in a
        row, so one still, camera-facing jellyfish can't monopolize every
        input forever -- a separate trigger from (and doesn't touch) the
        normal lost-track re-acquisition path above.

        Returns (switched: bool, old_centroid, new_centroid_or_None).
        switched is False, with the existing lock left untouched, if not
        currently locked or if no other qualifying candidate exists in the
        current frame (e.g. only one jellyfish is facing the camera right
        now) -- callers should treat that as "try again next fire", not an
        error.
        """
        state = self.state
        if not state.locked or state.raw_centroid is None:
            return False, None, None

        old_centroid = state.raw_centroid
        ox, oy = old_centroid
        alternatives = [
            c for c in self._last_candidates
            if math.hypot(c.centroid[0] - ox, c.centroid[1] - oy) >= config.FORCED_ROTATION_MIN_DISTANCE
        ]
        if not alternatives:
            return False, old_centroid, None

        new_candidate = max(alternatives, key=lambda c: c.score)

        # Snap (don't smooth) onto the new candidate -- this is a
        # deliberate hard switch to a different jellyfish, potentially far
        # away, not a continuation of the same one drifting; smoothing
        # would drag the used centroid slowly toward it over several
        # frames instead of rotating immediately. Mirrors how update()
        # itself skips smoothing for a lock's very first frame.
        state.raw_centroid = new_candidate.centroid
        state.centroid = new_candidate.centroid
        state.last_candidate = new_candidate
        state.lost_frames = 0
        state.last_qualifying_time = now
        state.consecutive_no_detection_frames = 0

        log.info(
            "Forced rotation: switching lock from %.0f,%.0f to %.0f,%.0f (score=%.2f) "
            "after repeat-input threshold reached",
            old_centroid[0], old_centroid[1],
            new_candidate.centroid[0], new_candidate.centroid[1], new_candidate.score,
        )
        return True, old_centroid, new_candidate.centroid

    def seconds_since_last_qualifying(self, now: float) -> float | None:
        if self.state.last_qualifying_time is None:
            return None
        return now - self.state.last_qualifying_time
