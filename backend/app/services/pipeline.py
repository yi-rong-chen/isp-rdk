from __future__ import annotations

import logging
import queue
import threading
import time

import cv2

from app.core.config import settings
from app.services.camera import CameraConfig, MipiCamera
from app.services.video_recorder import RecorderConfig, VideoRecorder
from app.services.yolo_infer import YOLODetector, draw_detection

logger = logging.getLogger("RDK_PIPELINE")


def _align_to_8(value: int) -> int:
    return ((value + 7) // 8) * 8


class InferencePipeline:
    def __init__(self) -> None:
        self.detector = YOLODetector(
            model_file=settings.model_path,
            classes_num=settings.classes_num,
            reg=settings.reg,
            conf=settings.conf_thres,
            iou=settings.iou_thres,
        )

        sensor_width = settings.sensor_width
        sensor_height = settings.sensor_height
        if settings.record_enabled and settings.record_encode_type in (1, 2):
            aligned_w = _align_to_8(sensor_width)
            aligned_h = _align_to_8(sensor_height)
            if aligned_w != sensor_width or aligned_h != sensor_height:
                logger.warning(
                    "display resolution %dx%d not 8-aligned for recorder codec=%d, use %dx%d",
                    sensor_width,
                    sensor_height,
                    settings.record_encode_type,
                    aligned_w,
                    aligned_h,
                )
                sensor_width, sensor_height = aligned_w, aligned_h

        self.camera = MipiCamera(
            CameraConfig(
                camera_index=settings.camera_index,
                sensor_width=sensor_width,
                sensor_height=sensor_height,
                infer_width=settings.infer_width,
                infer_height=settings.infer_height,
                stream_fps=settings.stream_fps,
                grab_x=settings.grab_x,
                grab_y=settings.grab_y,
                grab_w=settings.grab_w,
                grab_h=settings.grab_h,
                brightness_delta=settings.brightness_delta,
            )
        )

        self.recorder = None
        if settings.record_enabled:
            self.recorder = VideoRecorder(
                RecorderConfig(
                    output_dir=settings.record_dir,
                    width=settings.grab_w,
                    height=settings.grab_h,
                    segment_seconds=settings.record_segment_seconds,
                    encode_type=settings.record_encode_type,
                    bitrate_kbps=settings.record_bitrate_kbps,
                )
            )

        self._record_interval_sec = 0.0 if settings.record_fps <= 0 else 1.0 / settings.record_fps
        self._next_record_ts = 0.0
        self._process_interval_sec = 0.0 if settings.process_fps <= 0 else 1.0 / settings.process_fps
        self._camera_read_interval_sec = 0.0 if settings.camera_read_fps <= 0 else 1.0 / settings.camera_read_fps
        self._next_camera_read_ts = 0.0

        self._frame_count = 0
        self._fps = 0.0
        self._fps_start = time.time()

        self._running = False
        self._annotated_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self._latest_annotated = None
        self._annotated_lock = threading.Lock()
        self._latest_detection = {
            "timestamp": 0.0,
            "total": 0,
            "counts": {},
            "boxes": [],
        }
        self._detection_lock = threading.Lock()

        self._latest_jpeg = None
        self._jpeg_quality = settings.jpeg_quality
        self._jpeg_cond = threading.Condition()
        self._jpeg_seq = 0

        self._annotate_thread: threading.Thread | None = None
        self._jpeg_thread: threading.Thread | None = None
        self._thread_error: str | None = None
        self._detection_callback = None

    def _should_enqueue_record(self) -> bool:
        if self._record_interval_sec <= 0:
            return True
        now = time.monotonic()
        if now >= self._next_record_ts:
            self._next_record_ts = now + self._record_interval_sec
            return True
        return False

    def _tick_fps(self) -> float:
        self._frame_count += 1
        if self._frame_count >= 30:
            now = time.time()
            dt = now - self._fps_start
            if dt > 0:
                self._fps = self._frame_count / dt
            self._frame_count = 0
            self._fps_start = now
        return self._fps

    def _build_annotated_frame(self):
        if self._camera_read_interval_sec > 0:
            now = time.monotonic()
            if now < self._next_camera_read_ts:
                time.sleep(self._next_camera_read_ts - now)
            self._next_camera_read_ts = max(
                self._next_camera_read_ts + self._camera_read_interval_sec,
                time.monotonic(),
            )
        display_nv12 = self.camera.read_display_nv12()
        infer_nv12 = self.camera.read_infer_nv12()
        while display_nv12 is None or infer_nv12 is None:
            if not self._running:
                return None
            display_nv12 = self.camera.read_display_nv12()
            infer_nv12 = self.camera.read_infer_nv12()

        cfg = self.camera.config
        ids, scores, bboxes = self.detector.infer_nv12(
            infer_nv12,
            width=cfg.infer_width,
            height=cfg.infer_height,
        )

        frame = self.camera.nv12_to_bgr(display_nv12, cfg.grab_w, cfg.grab_h)
        scale_x = cfg.grab_w / cfg.infer_width
        scale_y = cfg.grab_h / cfg.infer_height
        counts: dict[str, int] = {}
        boxes = []

        for class_id, score, bbox in zip(ids, scores, bboxes):
            cid = int(class_id)
            x1, y1, x2, y2 = [int(v) for v in bbox]
            mapped = (
                int(x1 * scale_x),
                int(y1 * scale_y),
                int(x2 * scale_x),
                int(y2 * scale_y),
            )
            key = str(cid)
            counts[key] = counts.get(key, 0) + 1
            boxes.append(
                {
                    "class_id": cid,
                    "score": float(score),
                    "bbox": [mapped[0], mapped[1], mapped[2], mapped[3]],
                }
            )
            draw_detection(frame, mapped, float(score), cid)

        with self._detection_lock:
            self._latest_detection = {
                "timestamp": time.time(),
                "total": len(boxes),
                "counts": counts,
                "boxes": boxes,
            }
        if self._detection_callback is not None:
            try:
                self._detection_callback(self._latest_detection)
            except Exception:
                logger.exception("detection callback failed")

        if self.recorder is not None and self._should_enqueue_record():
            self.recorder.enqueue_nv12(display_nv12)

        fps = self._tick_fps()
        cv2.putText(
            frame,
            f"FPS: {fps:.1f}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        return frame

    def _put_latest(self, frame) -> None:
        try:
            self._annotated_queue.put_nowait(frame)
            return
        except queue.Full:
            pass
        try:
            self._annotated_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._annotated_queue.put_nowait(frame)
        except queue.Full:
            pass

    def _annotate_loop(self) -> None:
        next_ts = time.monotonic()
        while self._running:
            try:
                if self._process_interval_sec > 0:
                    now = time.monotonic()
                    if now < next_ts:
                        time.sleep(next_ts - now)
                    next_ts = max(next_ts + self._process_interval_sec, time.monotonic())
                frame = self._build_annotated_frame()
                if frame is None:
                    continue
                with self._annotated_lock:
                    self._latest_annotated = frame
                self._put_latest(frame)
            except Exception as exc:
                self._thread_error = f"annotate loop error: {exc}"
                logger.exception("annotate loop error")
                time.sleep(0.05)

    def _jpeg_loop(self) -> None:
        while self._running:
            try:
                try:
                    item = self._annotated_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break

                frame = item
                quality = self._jpeg_quality
                ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if not ok:
                    continue

                with self._jpeg_cond:
                    self._latest_jpeg = encoded.tobytes()
                    self._jpeg_seq += 1
                    self._jpeg_cond.notify_all()
            except Exception as exc:
                self._thread_error = f"jpeg loop error: {exc}"
                logger.exception("jpeg loop error")
                time.sleep(0.05)

    def start(self) -> None:
        self.camera.open()
        if self.recorder is not None:
            try:
                self.recorder.start()
            except Exception as exc:
                logger.error("recorder start failed, continue without recording: %s", exc)
                self.recorder = None

        self._running = True
        self._annotate_thread = threading.Thread(target=self._annotate_loop, name="annotate-loop", daemon=True)
        self._jpeg_thread = threading.Thread(target=self._jpeg_loop, name="jpeg-loop", daemon=True)
        self._annotate_thread.start()
        self._jpeg_thread.start()

    def stop(self) -> None:
        self._running = False

        if self._annotate_thread is not None:
            self._annotate_thread.join(timeout=1.0)
            self._annotate_thread = None

        try:
            self._annotated_queue.put_nowait(None)
        except queue.Full:
            pass

        if self._jpeg_thread is not None:
            self._jpeg_thread.join(timeout=1.0)
            self._jpeg_thread = None

        if self.recorder is not None:
            self.recorder.stop()
        self.camera.close()

    def next_annotated_frame(self):
        with self._annotated_lock:
            frame = self._latest_annotated
        if frame is None:
            raise RuntimeError("no annotated frame available yet")
        return frame

    def get_latest_detections(self) -> dict:
        with self._detection_lock:
            # Return a shallow copy to avoid external mutation.
            return {
                "timestamp": self._latest_detection["timestamp"],
                "total": self._latest_detection["total"],
                "counts": dict(self._latest_detection["counts"]),
                "boxes": list(self._latest_detection["boxes"]),
            }

    def set_detection_callback(self, callback) -> None:
        self._detection_callback = callback

    def next_jpeg(self, quality: int, last_seq: int | None = None, timeout: float = 2.0) -> tuple[bytes, int]:
        if quality != self._jpeg_quality:
            self._jpeg_quality = quality

        with self._jpeg_cond:
            if self._latest_jpeg is None or (last_seq is not None and self._jpeg_seq == last_seq):
                ok = self._jpeg_cond.wait(timeout=timeout)
                if not ok or self._latest_jpeg is None:
                    # Fallback: generate one frame synchronously to avoid stream deadlock.
                    frame = self._build_annotated_frame()
                    if frame is not None:
                        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                        if ok:
                            self._latest_jpeg = encoded.tobytes()
                            self._jpeg_seq += 1
                            return self._latest_jpeg, self._jpeg_seq
                    detail = self._thread_error or "no jpeg frame available yet"
                    raise RuntimeError(detail)
            return self._latest_jpeg, self._jpeg_seq
