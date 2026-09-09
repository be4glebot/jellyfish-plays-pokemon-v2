"""
Entry point: wires stream capture, tracking, zone mapping, the TCP bridge,
and the dashboard together, and runs the main processing loop.

Run with: python main.py
Stop with: Ctrl+C (graceful shutdown -- closes sockets, releases capture,
stops threads).
"""

import logging
import signal
import threading
import time

import cv2

import config
import dashboard
from stream_capture import StreamCapture
from tcp_server import CommandServer
from tracker import JellyfishTracker, compute_roi
from zone_mapper import ZoneMapper

log = logging.getLogger("main")


def setup_logging(shared_state: dashboard.SharedState):
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL), format=config.LOG_FORMAT)
    error_handler = dashboard.ErrorLogHandler(shared_state)
    error_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logging.getLogger().addHandler(error_handler)


def draw_full_frame_overlay(frame, roi_bounds):
    """Full frame with just the ROI boundary marked -- a lightweight
    reference thumbnail for the /status debug page. The primary page's grid
    and tracking annotations are drawn separately, on the ROI crop, since
    that's what /video_feed_roi actually streams."""
    annotated = frame.copy()
    x0, y0, x1, y1 = roi_bounds
    cv2.rectangle(annotated, (x0, y0), (x1, y1), (0, 255, 255), 2)
    return annotated


def draw_roi_overlay(roi, tracker_state, chosen_candidate):
    """The ROI crop with the tracked contour + smoothed centroid drawn on
    it. This is what /video_feed_roi streams; the 3x3 button grid is
    composited client-side (CSS) on top of it in the primary page so it
    stays crisp and independent of video resolution/scaling."""
    annotated = roi.copy()
    if chosen_candidate is not None:
        cv2.drawContours(annotated, [chosen_candidate.contour], -1, (0, 255, 0), 2)
    if tracker_state.centroid is not None:
        cx, cy = tracker_state.centroid
        cv2.circle(annotated, (int(cx), int(cy)), 6, (0, 0, 255), -1)
    return annotated


def main():
    shared_state = dashboard.SharedState()
    setup_logging(shared_state)
    log.info("Starting Jellyfish Plays Pokemon")

    capture = StreamCapture()
    tracker = JellyfishTracker()
    zone_mapper = ZoneMapper()
    tcp_server = CommandServer()

    stop_event = threading.Event()

    def handle_sigint(signum, frame):
        log.info("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        signal.signal(signal.SIGTERM, handle_sigint)
    except (ValueError, AttributeError):
        pass  # SIGTERM not available on this platform

    tcp_server.start()
    capture.start()

    dashboard_thread = threading.Thread(
        target=dashboard.run_dashboard, args=(shared_state,), name="Dashboard", daemon=True,
    )
    dashboard_thread.start()

    frame_times = []
    full_frame_counter = 0
    FULL_FRAME_ENCODE_EVERY_N = 5

    # Per-stage timing diagnostics: accumulate wall-clock time spent in each
    # phase of the loop and log an averaged breakdown periodically, so a slow
    # loop can be attributed to a specific stage (capture wait / CV detection
    # / draw+encode) instead of guessed at.
    stage_totals = {"capture_wait": 0.0, "detect": 0.0, "decide_send": 0.0, "draw_encode": 0.0}
    stage_count = 0
    STAGE_LOG_EVERY_N = 30

    try:
        while not stop_event.is_set():
            loop_start = time.monotonic()

            t0 = time.monotonic()
            frame = capture.get_latest_frame(timeout=1.0)
            t1 = time.monotonic()
            stage_totals["capture_wait"] += t1 - t0

            shared_state.update_status(
                stream_state=capture.state,
                mgba_connected=tcp_server.client_connected,
            )

            if frame is None:
                continue

            x0, y0, x1, y1 = compute_roi(frame.shape)
            roi = frame[y0:y1, x0:x1]
            roi_h, roi_w = roi.shape[:2]

            now = time.monotonic()
            centroid, chosen, _debug_mask, detection_stats = tracker.update(roi, now)
            seconds_since_qualifying = tracker.seconds_since_last_qualifying(now)
            t2 = time.monotonic()
            stage_totals["detect"] += t2 - now

            # tracker.state.centroid deliberately holds its last-known value
            # even after a lock is fully dropped (see tracker.update()'s
            # comment on TRACK_MAX_LOST_FRAMES) -- useful for the debug
            # overlay's dot, but feeding that stale, frozen position into the
            # zone/input decision would make it look like a jellyfish is
            # still sitting there and keep firing debounced inputs from it
            # indefinitely, long after detection has genuinely stopped
            # (confirmed live: 8 consecutive B presses over 18s while
            # tracker.state.locked was False the whole time and zero
            # candidates were being found in any frame).
            #
            # Gating on tracker.state.locked alone isn't tight enough,
            # though: it stays True for up to TRACK_MAX_LOST_FRAMES (15)
            # frames after a real candidate was last actually found, using
            # the same frozen position that whole grace window. With
            # DEBOUNCE_FRAMES now as low as 3, a brief few-frame detection
            # gap (common -- confirmed live that even a clearly-visible bell
            # doesn't qualify on every single frame) is enough to satisfy
            # debounce entirely off a stale position, firing on wherever the
            # jellyfish *was* -- which can look like empty water if it's
            # since moved or the camera panned, while a different jellyfish
            # is clearly visible elsewhere. Gate on chosen instead: only
            # non-None on frames where a real candidate was actually matched
            # this exact frame, a strict subset of state.locked that closes
            # this gap. shared_state below still gets the raw (possibly
            # stale) centroid for display.
            decision = zone_mapper.decide(
                centroid if chosen is not None else None,
                roi_w, roi_h, seconds_since_qualifying, now,
            )

            if decision["button"]:
                tcp_server.send_command(decision["button"])
                shared_state.record_input(decision["button"], decision["source"], decision["zone"])

            if decision["force_rotate"]:
                switched, old_centroid, new_centroid = tracker.force_rotate(
                    now, roi_w, roi_h, decision["zone"],
                )
                zone_mapper.notify_rotation_outcome(switched)
                if switched:
                    shared_state.record_rotation(
                        old_centroid, new_centroid, decision["button"], decision["zone"],
                    )
                else:
                    log.info(
                        "Forced rotation requested but no other qualifying candidate "
                        "this frame -- keeping current lock, will retry next fire"
                    )
            t3 = time.monotonic()
            stage_totals["decide_send"] += t3 - t2

            roi_annotated = draw_roi_overlay(roi, tracker.state, chosen)
            shared_state.set_frame_roi(roi_annotated)

            # The full-frame thumbnail is only ever viewed on /status (a
            # debug page, not the primary stream feed), so encoding it every
            # single loop iteration was pure wasted CPU on the hot path that
            # also gates decision speed (DEBOUNCE_FRAMES counts loop
            # iterations) and the primary video feed's frame rate. A few
            # updates/sec is plenty for a reference thumbnail.
            full_frame_counter += 1
            if full_frame_counter >= FULL_FRAME_ENCODE_EVERY_N:
                full_frame_counter = 0
                full_annotated = draw_full_frame_overlay(frame, (x0, y0, x1, y1))
                shared_state.set_frame_full(full_annotated)
            t4 = time.monotonic()
            stage_totals["draw_encode"] += t4 - t3

            stage_count += 1
            if stage_count >= STAGE_LOG_EVERY_N:
                log.info(
                    "Stage breakdown (avg over %d iters, ms): capture_wait=%.1f detect=%.1f "
                    "decide_send=%.1f draw_encode=%.1f",
                    stage_count,
                    1000 * stage_totals["capture_wait"] / stage_count,
                    1000 * stage_totals["detect"] / stage_count,
                    1000 * stage_totals["decide_send"] / stage_count,
                    1000 * stage_totals["draw_encode"] / stage_count,
                )
                stage_totals = {k: 0.0 for k in stage_totals}
                stage_count = 0

            loop_end = time.monotonic()
            frame_times.append(loop_end - loop_start)
            if len(frame_times) > 30:
                frame_times.pop(0)
            avg_dt = sum(frame_times) / len(frame_times)
            shared_state.update_status(
                raw_zone=decision["zone"],
                current_zone=decision["zone"] if decision["button"] and decision["source"] == "jellyfish" else shared_state.current_zone,
                debounce_progress=decision["debounce_progress"],
                pending_frames=decision["pending_frames"],
                centroid=centroid,
                auto_idle=decision["auto_idle"],
                auto_idle_activation_count=zone_mapper.auto_idle_activation_count,
                fps=(1.0 / avg_dt) if avg_dt > 0 else 0.0,
                latency_ms=(loop_end - loop_start) * 1000.0,
                last_frame_time=time.time(),
            )

    finally:
        log.info("Shutting down...")
        capture.stop()
        tcp_server.stop()
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
