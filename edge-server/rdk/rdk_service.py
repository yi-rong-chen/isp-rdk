#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import queue
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

from ipc_labels import LabelsPublisher
from logger_config import get_logger

try:
    from hobot_vio import libsrcampy as srcampy
except Exception:  # pragma: no cover
    srcampy = None

try:
    from hobot_dnn import pyeasy_dnn as dnn
except Exception:  # pragma: no cover
    dnn = None


logger = get_logger(__name__, log_name="rdk")

LABELED_VIDEO_BITRATE_FACTOR = 0.01
MIN_DETECTOR_CONF_THRESHOLD = 1e-6
MAX_DETECTOR_CONF_THRESHOLD = 1.0 - 1e-6
BASE_SENSOR_WIDTH = 1920
BASE_SENSOR_HEIGHT = 1080
DEFAULT_SENSOR_SCALE = 1280 / BASE_SENSOR_WIDTH

def _align_to_8(value: int) -> int:
    return ((int(value) + 7) // 8) * 8


def _align_to_even(value: float) -> int:
    return max(2, int(float(value) / 2.0 + 0.5) * 2)


def _half_frame_size(width: int, height: int) -> tuple[int, int]:
    return max(1, int(width) // 2), max(1, int(height) // 2)


def _coerce_positive_int(value: object) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_positive_float(value: object) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _sanitize_bitrate_coefficient(value: object, default: float = 1.0) -> float:
    coefficient = _coerce_positive_float(value)
    return coefficient if coefficient is not None else float(default)


def _normalize_probability(value: object, default: float = 0.25) -> float:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        probability = float(default)
    return max(0.0, min(1.0, probability))


def _normalize_labels_confs(labels_confs: object) -> dict[str, float]:
    if not isinstance(labels_confs, dict):
        return {}

    normalized_confs = {}
    for raw_label, raw_conf in labels_confs.items():
        label = str(raw_label).strip()
        if not label:
            continue
        normalized_confs[label] = _normalize_probability(raw_conf, default=0.0)
    return normalized_confs


def _sanitize_detector_conf_threshold(value: object, default: float = 0.25) -> float:
    threshold = _normalize_probability(value, default)
    return min(MAX_DETECTOR_CONF_THRESHOLD, max(MIN_DETECTOR_CONF_THRESHOLD, threshold))


def _estimate_encoder_bitrate_kbps(width: int, height: int, fps: float, coefficient: float = 1.0) -> int:
    width = max(1, int(width))
    height = max(1, int(height))
    fps = max(1.0, float(fps))
    coefficient = _sanitize_bitrate_coefficient(coefficient, 1.0)
    bitrate = width * height * fps * 0.1 * coefficient / 60
    return max(1, int(round(bitrate)))


def _resolve_sensor_scale(camera_cfg: dict, infer_cfg: dict, engine_settings: dict) -> float:
    for raw_value in (
        camera_cfg.get("sensor_scale"),
        infer_cfg.get("sensor_scale"),
        engine_settings.get("sensor_scale"),
    ):
        sensor_scale = _coerce_positive_float(raw_value)
        if sensor_scale is not None:
            return sensor_scale

    legacy_sensor_width = _coerce_positive_int(
        camera_cfg.get("sensor_width", infer_cfg.get("sensor_width", engine_settings.get("sensor_width", camera_cfg.get("width"))))
    )
    if legacy_sensor_width is not None:
        return legacy_sensor_width / BASE_SENSOR_WIDTH

    legacy_sensor_height = _coerce_positive_int(
        camera_cfg.get("sensor_height", infer_cfg.get("sensor_height", engine_settings.get("sensor_height", camera_cfg.get("height"))))
    )
    if legacy_sensor_height is not None:
        return legacy_sensor_height / BASE_SENSOR_HEIGHT

    return DEFAULT_SENSOR_SCALE


def _resolve_display_dimensions(sensor_scale: float) -> tuple[int, int]:
    display_width = _align_to_even(BASE_SENSOR_WIDTH * sensor_scale)
    display_height = _align_to_even(BASE_SENSOR_HEIGHT * sensor_scale)
    return display_width, display_height


def _resolve_infer_dimensions(camera_cfg: dict, infer_cfg: dict, engine_settings: dict) -> tuple[int, int]:
    img_size = _coerce_positive_int(
        camera_cfg.get("img_size", infer_cfg.get("img_size", engine_settings.get("img_size")))
    )
    if img_size is not None:
        return img_size, img_size

    target_resolution = camera_cfg.get("target_resolution")
    infer_width = infer_cfg.get("infer_width", engine_settings.get("infer_width"))
    infer_height = infer_cfg.get("infer_height", engine_settings.get("infer_height"))
    if isinstance(target_resolution, dict):
        infer_width = target_resolution.get("width", infer_width)
        infer_height = target_resolution.get("height", infer_height)

    infer_width = _coerce_positive_int(infer_width)
    if infer_width is None:
        infer_width = _coerce_positive_int(camera_cfg.get("infer_width")) or 640

    infer_height = _coerce_positive_int(infer_height)
    if infer_height is None:
        infer_height = _coerce_positive_int(camera_cfg.get("infer_height")) or 640

    return infer_width, infer_height


def _softmax(data: np.ndarray, axis: int) -> np.ndarray:
    shifted = data - np.max(data, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=axis, keepdims=True)


def _resolve_ffmpeg_bin() -> str | None:
    candidates = [
        "/usr/local/ffmpeg/bin/ffmpeg",
        shutil.which("ffmpeg"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _suffix_for_encode_type(encode_type: int) -> str:
    return {
        1: ".h264",
        2: ".h265",
        3: ".mjpg",
    }.get(int(encode_type), ".bin")


@dataclass(frozen=True)
class HardwareRecorderConfig:
    output_dir: Path
    prefix: str
    width: int
    height: int
    fps: float
    segment_seconds: int
    bitrate_kbps: int
    encode_type: int = 2  # 1: H264, 2: H265, 3: MJPEG
    video_chn: int = 1
    queue_size: int = 8


class HardwareSegmentedRecorder:
    def __init__(self, config: HardwareRecorderConfig):
        if srcampy is None:
            raise RuntimeError("hobot_vio.libsrcampy 不可用，无法启用硬编码录像")

        self.config = config
        self.output_dir = Path(config.output_dir)
        self.prefix = str(config.prefix)
        self.width = int(config.width)
        self.height = int(config.height)
        self.fps = max(1.0, float(config.fps))
        self.segment_seconds = max(1, int(config.segment_seconds))
        self.encode_type = int(config.encode_type)
        self.bitrate_kbps = _coerce_positive_int(config.bitrate_kbps)
        self.video_chn = int(config.video_chn)
        self.queue_size = max(1, int(config.queue_size))
        self.encoder_width = _align_to_8(self.width) if self.encode_type in (1, 2) else self.width
        self.encoder_height = _align_to_8(self.height) if self.encode_type in (1, 2) else self.height
        self.raw_suffix = _suffix_for_encode_type(self.encode_type)
        self.ffmpeg_bin = _resolve_ffmpeg_bin()

        self._encoder = None
        self._outfile = None
        self._worker = None
        self._running = False
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=self.queue_size)
        self._remux_threads: list[threading.Thread] = []
        self._remux_lock = threading.Lock()

        self._current_raw_path = None
        self._current_started_at = 0
        self._current_opened_at = 0.0
        self._current_frame_count = 0
        self._current_dropped_frames = 0
        self._last_started_at = 0

    def start(self):
        if self._running:
            return

        encoder_cls = getattr(srcampy, "Encoder", None) or getattr(srcampy, "Encode", None)
        if encoder_cls is None:
            raise RuntimeError("No Encoder/Encode class found in hobot_vio.libsrcampy")

        self._encoder = encoder_cls()
        ret = self._encoder.encode(
            self.video_chn,
            self.encode_type,
            self.encoder_width,
            self.encoder_height,
            self.bitrate_kbps,
        )
        if ret != 0:
            raise RuntimeError(
                f"failed to start record encoder: ret={ret}, chn={self.video_chn}, "
                f"encode_type={self.encode_type}, resolution={self.encoder_width}x{self.encoder_height}, "
                f"bitrate={self.bitrate_kbps}"
            )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._open_new_segment(int(time.time()))

        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True, name=f"record-{self.prefix}")
        self._worker.start()
        logger.info(
            "[HW_RECORDER] started prefix=%s chn=%d encode_type=%d input=%dx%d encoder=%dx%d fps=%.2f segment=%ds bitrate=%d queue=%d ffmpeg=%s",
            self.prefix,
            self.video_chn,
            self.encode_type,
            self.width,
            self.height,
            self.encoder_width,
            self.encoder_height,
            self.fps,
            self.segment_seconds,
            self.bitrate_kbps,
            self.queue_size,
            self.ffmpeg_bin or "not_found",
        )

    def stop(self):
        if not self._running:
            return

        self._running = False
        self._enqueue_command("stop", None, timeout=2.0)
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None

        self._join_remux_threads()

        if self._encoder is not None:
            ret = self._encoder.close()
            if ret != 0:
                logger.warning("[HW_RECORDER] encoder close returned non-zero: %s", ret)
            self._encoder = None

        logger.info("[HW_RECORDER] stopped prefix=%s", self.prefix)

    def write_nv12(self, nv12: np.ndarray):
        if not self._running:
            return

        self._enqueue_frame("frame_nv12", nv12.copy())

    def write_bgr(self, frame: np.ndarray):
        if not self._running:
            return

        self._enqueue_frame("frame_bgr", frame.copy())

    def split_now(self):
        if not self._running:
            return None

        event = threading.Event()
        result = {"filename": None}
        self._enqueue_command("split", (event, result), timeout=5.0)
        event.wait(timeout=15.0)
        return result.get("filename")

    def _enqueue_frame(self, kind: str, payload: np.ndarray):
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            self._current_dropped_frames += 1
            logger.warning(
                "[HW_RECORDER] queue full, dropped frame prefix=%s dropped=%d qsize=%d",
                self.prefix,
                self._current_dropped_frames,
                self._queue.qsize(),
            )

    def _enqueue_command(self, kind: str, payload: object, timeout: float):
        while True:
            try:
                self._queue.put((kind, payload), timeout=timeout)
                return
            except queue.Full:
                if not self._running and kind != "stop":
                    return

    def _run(self):
        while self._running or not self._queue.empty():
            try:
                kind, payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if kind == "stop":
                break

            if kind == "split":
                event, result = payload
                result["filename"] = (
                    self._rotate_segment(int(time.time()), wait_for_remux=True)
                    if self._current_frame_count > 0
                    else None
                )
                event.set()
                continue

            if time.time() - self._current_opened_at >= self.segment_seconds and self._current_frame_count > 0:
                self._rotate_segment(int(time.time()), wait_for_remux=False)

            if kind == "frame_nv12":
                self._encode_nv12(payload)
            elif kind == "frame_bgr":
                self._encode_bgr(payload)

        self._close_current_segment(wait_for_remux=True)

    def _encode_bgr(self, frame: np.ndarray):
        nv12 = MipiCamera.bgr_to_nv12(frame, self.encoder_width, self.encoder_height)
        self._encode_bytes(nv12)

    def _encode_nv12(self, nv12: np.ndarray):
        expected_input_size = self.width * self.height * 3 // 2
        if nv12.size != expected_input_size:
            logger.warning(
                "[HW_RECORDER] skip frame due to input size mismatch prefix=%s got=%d expected=%d",
                self.prefix,
                nv12.size,
                expected_input_size,
            )
            return

        if self.width != self.encoder_width or self.height != self.encoder_height:
            bgr = MipiCamera.nv12_to_bgr(nv12, self.width, self.height)
            nv12 = MipiCamera.bgr_to_nv12(bgr, self.encoder_width, self.encoder_height)

        self._encode_bytes(nv12)

    def _encode_bytes(self, nv12: np.ndarray):
        if self._encoder is None or self._outfile is None:
            return

        ret = self._encoder.encode_file(nv12.tobytes())
        if ret != 0:
            logger.warning("[HW_RECORDER] encode_file failed prefix=%s ret=%s", self.prefix, ret)
            return

        packet = self._encoder.get_img()
        if packet is not None:
            self._outfile.write(packet)
        self._current_frame_count += 1

    def _open_new_segment(self, started_at: int):
        if started_at <= self._last_started_at:
            started_at = self._last_started_at + 1

        raw_name = f"{self.prefix}_{started_at}{self.raw_suffix}"
        raw_path = self.output_dir / raw_name
        self._outfile = open(raw_path, "wb")
        self._current_raw_path = raw_path
        self._current_started_at = started_at
        self._current_opened_at = time.time()
        self._current_frame_count = 0
        self._current_dropped_frames = 0
        self._last_started_at = started_at
        logger.info(
            "[HW_RECORDER] opened %s prefix=%s chn=%d encode_type=%d",
            raw_path,
            self.prefix,
            self.video_chn,
            self.encode_type,
        )

    def _rotate_segment(self, started_at: int, wait_for_remux: bool):
        completed = self._close_current_segment(wait_for_remux=wait_for_remux)
        self._open_new_segment(started_at)
        return completed

    def _close_current_segment(self, wait_for_remux: bool):
        completed_name = None
        if self._outfile is not None:
            self._outfile.flush()
            self._outfile.close()
            self._outfile = None

        if self._current_raw_path is not None:
            wall_elapsed = max(time.time() - self._current_opened_at, 0.0)
            effective_fps = (self._current_frame_count / wall_elapsed) if wall_elapsed > 0 else 0.0
            playback_seconds = (self._current_frame_count / self.fps) if self.fps > 0 else 0.0
            if self._current_frame_count > 0:
                completed_name = self._complete_segment(
                    self._current_raw_path,
                    effective_fps,
                    wait_for_remux=wait_for_remux,
                )
            else:
                try:
                    self._current_raw_path.unlink(missing_ok=True)
                except Exception:
                    pass

            logger.info(
                "[HW_RECORDER] closed %s frames=%d wall=%.2fs effective_fps=%.2f target_fps=%.2f playback=%.2fs dropped=%d completed=%s",
                self._current_raw_path.name,
                self._current_frame_count,
                wall_elapsed,
                effective_fps,
                self.fps,
                playback_seconds,
                self._current_dropped_frames,
                completed_name,
            )

        self._current_raw_path = None
        self._current_started_at = 0
        self._current_opened_at = 0.0
        self._current_frame_count = 0
        self._current_dropped_frames = 0
        return completed_name

    def _complete_segment(self, raw_path: Path, effective_fps: float, wait_for_remux: bool):
        if wait_for_remux:
            return self._remux_to_mp4(raw_path, effective_fps)

        if self.encode_type not in (1, 2) or self.ffmpeg_bin is None:
            return raw_path.name

        output_name = raw_path.with_suffix(".mp4").name

        def _worker():
            self._remux_to_mp4(raw_path, effective_fps)

        thread = threading.Thread(
            target=_worker,
            daemon=True,
            name=f"remux-{self.prefix}-{raw_path.stem}",
        )
        with self._remux_lock:
            self._remux_threads = [item for item in self._remux_threads if item.is_alive()]
            self._remux_threads.append(thread)
        thread.start()
        return output_name

    def _join_remux_threads(self):
        with self._remux_lock:
            threads = list(self._remux_threads)
            self._remux_threads = []
        for thread in threads:
            thread.join(timeout=10.0)

    def _remux_to_mp4(self, raw_path: Path, effective_fps: float):
        if self.encode_type not in (1, 2) or self.ffmpeg_bin is None:
            logger.warning(
                "[HW_RECORDER] remux skipped prefix=%s path=%s encode_type=%d ffmpeg=%s",
                self.prefix,
                raw_path,
                self.encode_type,
                self.ffmpeg_bin,
            )
            return raw_path.name

        output_path = raw_path.with_suffix(".mp4")
        input_fps = max(effective_fps, 1.0)
        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-r",
            f"{input_fps:.6f}",
            "-i",
            str(raw_path),
            "-c",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
            raw_path.unlink(missing_ok=True)
            return output_path.name
        except Exception as exc:
            logger.warning("[HW_RECORDER] remux failed prefix=%s path=%s error=%s", self.prefix, raw_path, exc)
            return raw_path.name


@dataclass(frozen=True)
class MipiCameraConfig:
    camera_index: int
    display_chn: int
    infer_chn: int
    display_width: int
    display_height: int
    infer_width: int
    infer_height: int
    brightness_delta: int = 0


class MipiCamera:
    def __init__(self, config: MipiCameraConfig):
        if srcampy is None:
            raise RuntimeError("hobot_vio.libsrcampy 不可用，请在 RDK X5 设备上运行")

        self.config = config
        self._camera = srcampy.Camera()
        self._opened = False

    def open(self):
        if self._opened:
            return

        cfg = self.config
        try:
            ret = self._camera.open_cam(
                cfg.camera_index,
                -1,
                -1,
                [cfg.infer_width, cfg.display_width],
                [cfg.infer_height, cfg.display_height],
                1080,
                1920,
            )
        except TypeError:
            ret = self._camera.open_cam(
                cfg.camera_index,
                -1,
                -1,
                [cfg.infer_width, cfg.display_width],
                [cfg.infer_height, cfg.display_height],
                1080,
                1920,
            )

        if ret is not None and ret != 0:
            raise RuntimeError(
                f"open_cam failed: ret={ret}, camera_index={cfg.camera_index}, "
                f"display={cfg.display_width}x{cfg.display_height}, infer={cfg.infer_width}x{cfg.infer_height}"
            )

        self._opened = True
        if cfg.brightness_delta:
            logger.info("[CAMERA] software brightness delta enabled=%d", cfg.brightness_delta)
        logger.info(
            "[CAMERA] opened index=%d display_chn=%d infer_chn=%d display=%dx%d infer=%dx%d",
            cfg.camera_index,
            cfg.display_chn,
            cfg.infer_chn,
            cfg.display_width,
            cfg.display_height,
            cfg.infer_width,
            cfg.infer_height,
        )

    def close(self):
        if not self._opened:
            return
        self._camera.close_cam()
        self._opened = False
        logger.info("[CAMERA] closed")

    def read_display_nv12(self):
        return self._read_nv12(self.config.display_chn, self.config.display_width, self.config.display_height)

    def read_infer_nv12(self):
        return self._read_nv12(self.config.infer_chn, self.config.infer_width, self.config.infer_height)

    def _read_nv12(self, chn: int, width: int, height: int):
        if not self._opened:
            raise RuntimeError("camera is not opened")

        raw_bytes = self._camera.get_img(chn, width, height)
        if raw_bytes is None:
            return None
        brightness_delta = _coerce_int(self.config.brightness_delta) or 0
        if brightness_delta != 0:
            raw_bytes = self._adjust_nv12_brightness(raw_bytes, width, height, brightness_delta)
        return np.frombuffer(raw_bytes, dtype=np.uint8)

    @staticmethod
    def _adjust_nv12_brightness(nv12_bytes: bytes, width: int, height: int, delta: int) -> bytes:
        expected_size = width * height * 3 // 2
        nv12 = np.frombuffer(nv12_bytes, dtype=np.uint8)
        if nv12.size != expected_size:
            logger.warning(
                "[CAMERA] skip brightness delta=%d due to nv12 size mismatch: got=%d expected=%d",
                delta,
                nv12.size,
                expected_size,
            )
            return nv12_bytes

        y_size = width * height
        adjusted = np.empty_like(nv12)
        adjusted[:y_size] = np.clip(nv12[:y_size].astype(np.int16) + delta, 0, 255).astype(np.uint8)
        adjusted[y_size:] = nv12[y_size:]
        return adjusted.tobytes()

    @staticmethod
    def nv12_to_bgr(nv12: np.ndarray, width: int, height: int):
        expected_size = width * height * 3 // 2
        if nv12.size != expected_size:
            raise ValueError(
                f"NV12 buffer size mismatch: got={nv12.size}, expected={expected_size} for {width}x{height}"
            )
        nv12 = nv12.reshape((height * 3 // 2, width))
        return cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)

    @staticmethod
    def bgr_to_nv12(frame: np.ndarray, width: int, height: int):
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height))
        area = width * height
        yuv420p = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).reshape((area * 3 // 2,))
        y = yuv420p[:area]
        uv_planar = yuv420p[area:].reshape((2, area // 4))
        uv_packed = uv_planar.transpose((1, 0)).reshape((area // 2,))
        nv12 = np.empty_like(yuv420p)
        nv12[:area] = y
        nv12[area:] = uv_packed
        return nv12


class YOLODetector:
    def __init__(self, model_file: str, classes_num: int, reg: int, conf: float, iou: float):
        if dnn is None:
            raise RuntimeError("hobot_dnn.pyeasy_dnn 不可用，请在 RDK X5 设备上运行")

        begin = time.time()
        self.quantize_model = dnn.load(model_file)
        self.model = self.quantize_model[0]
        logger.info("[DNN] model loaded in %.2f ms", 1000 * (time.time() - begin))

        self.model_input_height, self.model_input_width = self.model.inputs[0].properties.shape[2:4]
        self.classes_num = int(classes_num)
        self.configured_classes_num = self.classes_num
        self.reg = int(reg)
        self.conf = float(conf)
        self.iou = float(iou)
        self.weights_static = np.arange(self.reg, dtype=np.float32)[np.newaxis, np.newaxis, :]
        self.pad_left = 0
        self.pad_top = 0
        self.scale = 1.0
        self.img_w = 0
        self.img_h = 0
        self.branches = self._build_branches()
        self.output_shapes = self._collect_output_shapes()
        detected_classes_num = self._infer_classes_num_from_outputs()
        if detected_classes_num is not None and detected_classes_num != self.classes_num:
            logger.warning(
                "[DNN] classes_num mismatch: configured=%d, detected=%d from model outputs. Using detected value.",
                self.classes_num,
                detected_classes_num,
            )
            self.classes_num = detected_classes_num
        self.conf_inverse = -np.log(1.0 / self.conf - 1.0)
        logger.info("[DNN] output shapes: %s", self.output_shapes)
        logger.info("[DNN] classes_num=%d reg=%d", self.classes_num, self.reg)

    def _collect_output_shapes(self):
        shapes = []
        for index, output in enumerate(self.model.outputs):
            shape = tuple(int(v) for v in output.properties.shape)
            shapes.append({"index": index, "shape": shape})
        return shapes

    def _infer_classes_num_from_outputs(self):
        candidate_channels = []
        for output in self.model.outputs:
            shape = output.properties.shape
            if len(shape) != 4:
                continue
            h, w, c = shape[1:]
            if h != w:
                continue
            if c != self.reg * 4:
                candidate_channels.append(int(c))

        if not candidate_channels:
            return None

        detected = max(candidate_channels)
        if len(set(candidate_channels)) != 1:
            logger.warning("[DNN] multiple non-bbox output channels found: %s, using %d", candidate_channels, detected)
        return detected

    def _build_branches(self):
        branches = {}
        for output in self.model.outputs:
            shape = output.properties.shape
            if len(shape) != 4:
                continue
            h, w, c = shape[1:]
            if h != w:
                continue

            if c == self.reg * 4:
                stride = self.model_input_width // h
                anchor = np.stack(
                    [
                        np.tile(np.linspace(0.5, h - 0.5, h), reps=h),
                        np.repeat(np.arange(0.5, h + 0.5, 1), h),
                    ],
                    axis=0,
                ).transpose(1, 0)
                branches[h] = {"stride": stride, "anchor": anchor}

        if not branches:
            raise ValueError("No bbox branches found from model outputs")

        logger.info("[DNN] detected branches: %s", sorted(branches.keys(), reverse=True))
        return branches

    def set_identity_geometry(self, width: int, height: int):
        self.img_w = int(width)
        self.img_h = int(height)
        self.pad_left = 0
        self.pad_top = 0
        self.scale = 1.0

    def forward(self, input_tensor: np.ndarray):
        outputs = self.model.forward(input_tensor)
        return [tensor.buffer for tensor in outputs]

    def _split_outputs(self, outputs):
        bbox_outputs = {}
        cls_outputs = {}
        cls_candidates = {}

        for output in outputs:
            if output.ndim != 4:
                continue
            h, w, c = output.shape[1:]
            if h != w:
                continue
            if c == self.reg * 4:
                bbox_outputs[h] = output.reshape(-1, self.reg * 4)
            else:
                cls_candidates.setdefault(int(c), {})[int(h)] = output

        if self.classes_num in cls_candidates:
            cls_outputs = {
                h: tensor.reshape(-1, self.classes_num)
                for h, tensor in cls_candidates[self.classes_num].items()
            }

        missing = [h for h in self.branches if h not in bbox_outputs or h not in cls_outputs]
        if missing and cls_candidates:
            inferred_classes_num = max(cls_candidates.keys())
            if inferred_classes_num != self.classes_num:
                logger.warning(
                    "[DNN] cls branch mismatch at runtime: current classes_num=%d, available cls channels=%s. Switching to %d.",
                    self.classes_num,
                    sorted(cls_candidates.keys()),
                    inferred_classes_num,
                )
                self.classes_num = inferred_classes_num
                cls_outputs = {
                    h: tensor.reshape(-1, self.classes_num)
                    for h, tensor in cls_candidates[self.classes_num].items()
                }
                missing = [h for h in self.branches if h not in bbox_outputs or h not in cls_outputs]

        if missing:
            raise ValueError(
                f"Missing output branches: {missing}, classes_num={self.classes_num}, "
                f"configured_classes_num={self.configured_classes_num}, "
                f"available_cls_channels={sorted(cls_candidates.keys())}, output_shapes={self.output_shapes}"
            )

        return bbox_outputs, cls_outputs

    def post_process(self, outputs):
        bbox_outputs, cls_outputs = self._split_outputs(outputs)
        all_boxes = []
        all_scores = []
        all_ids = []

        for branch_h, meta in self.branches.items():
            bboxes = bbox_outputs[branch_h]
            clses = cls_outputs[branch_h]

            max_scores = np.max(clses, axis=1)
            valid_indices = np.flatnonzero(max_scores >= self.conf_inverse)
            if len(valid_indices) == 0:
                continue

            ids = np.argmax(clses[valid_indices, :], axis=1)
            scores = 1.0 / (1.0 + np.exp(-max_scores[valid_indices]))
            bboxes_valid = bboxes[valid_indices, :]

            ltrb = np.sum(
                _softmax(bboxes_valid.reshape(-1, 4, self.reg), axis=2) * self.weights_static,
                axis=2,
            )
            anchor = meta["anchor"][valid_indices, :]
            stride = int(meta["stride"])

            x1y1 = anchor - ltrb[:, 0:2]
            x2y2 = anchor + ltrb[:, 2:4]
            dbboxes = np.hstack([x1y1, x2y2]) * stride

            all_boxes.append(dbboxes)
            all_scores.append(scores)
            all_ids.append(ids)

        if not all_boxes:
            return (
                np.empty((0,), dtype=np.int32),
                np.empty((0,), dtype=np.float32),
                np.empty((0, 4), dtype=np.int32),
            )

        dbboxes = np.concatenate(all_boxes, axis=0)
        scores = np.concatenate(all_scores, axis=0)
        ids = np.concatenate(all_ids, axis=0)

        indices = cv2.dnn.NMSBoxes(dbboxes.tolist(), scores.tolist(), self.conf, self.iou)
        if indices is None or len(indices) == 0:
            return (
                np.empty((0,), dtype=np.int32),
                np.empty((0,), dtype=np.float32),
                np.empty((0, 4), dtype=np.int32),
            )

        indices = np.array(indices).reshape(-1)
        bboxes = dbboxes[indices].copy()
        bboxes[:, [0, 2]] -= self.pad_left
        bboxes[:, [1, 3]] -= self.pad_top
        bboxes /= self.scale
        bboxes[:, [0, 2]] = np.clip(bboxes[:, [0, 2]], 0, max(self.img_w - 1, 0))
        bboxes[:, [1, 3]] = np.clip(bboxes[:, [1, 3]], 0, max(self.img_h - 1, 0))
        return ids[indices], scores[indices], bboxes.astype(np.int32)

    def infer_nv12(self, nv12: np.ndarray, width: int, height: int):
        if width != self.model_input_width or height != self.model_input_height:
            raise ValueError(
                f"infer_nv12 expects {self.model_input_width}x{self.model_input_height}, got {width}x{height}"
            )
        self.set_identity_geometry(width=width, height=height)
        outputs = self.forward(nv12)
        return self.post_process(outputs)


def draw_detection(img: np.ndarray, bbox: tuple[int, int, int, int], score: float, class_id: int, label_names: list[str]):
    x1, y1, x2, y2 = bbox
    color = (
        int((37 * (class_id + 3)) % 255),
        int((17 * (class_id + 7)) % 255),
        int((29 * (class_id + 11)) % 255),
    )
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)

    label = label_names[class_id] if 0 <= class_id < len(label_names) else f"cls_{class_id}"
    text = f"{label}:{score:.2f}"
    (text_w, text_h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    label_y = y1 - 6 if y1 - 6 > text_h else y1 + text_h + 6
    cv2.rectangle(img, (x1, label_y - text_h - 3), (x1 + text_w + 6, label_y + 3), color, cv2.FILLED)
    cv2.putText(img, text, (x1 + 3, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)


class RuntimeConfig:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        with open(self.config_path, "r", encoding="utf-8") as file:
            self.raw = json.load(file)

        camera_root = self.raw.get("camera", {})
        self.camera = self._normalize_camera_config(camera_root)
        self.infer = self.raw.get("infer", {})
        engine_root = self.raw.get("engine", {})
        self.engine_settings = engine_root.get("settings", {}) if isinstance(engine_root.get("settings"), dict) else {}
        self.runtime_settings = self._build_runtime_settings()

    @staticmethod
    def _normalize_camera_config(camera_root: dict) -> dict:
        camera_root = camera_root if isinstance(camera_root, dict) else {}

        mipi_config = camera_root.get("mipi")
        if isinstance(mipi_config, dict):
            return mipi_config

        normalized_camera = dict(camera_root)
        normalized_camera.pop("type", None)
        return normalized_camera

    def _build_runtime_settings(self):
        labels_confs = _normalize_labels_confs(self.infer.get("labels_confs"))
        configured_label_names = self.infer.get("label_names")
        if labels_confs:
            label_names = list(labels_confs.keys())
        elif isinstance(configured_label_names, list):
            label_names = [str(label).strip() for label in configured_label_names if str(label).strip()]
        else:
            label_names = []
        infer_width, infer_height = _resolve_infer_dimensions(self.camera, self.infer, self.engine_settings)
        default_conf_thres = _normalize_probability(
            self.infer.get("conf_thres", self.engine_settings.get("conf_thres", self.camera.get("conf_thres", 0.25))),
            0.25,
        )
        detector_conf_thres = default_conf_thres
        if labels_confs:
            detector_conf_thres = min(default_conf_thres, min(labels_confs.values()))
        detector_conf_thres = _sanitize_detector_conf_threshold(detector_conf_thres, default_conf_thres)

        sensor_scale = _resolve_sensor_scale(self.camera, self.infer, self.engine_settings)
        display_width, display_height = _resolve_display_dimensions(sensor_scale)
        default_stream_width, default_stream_height = _half_frame_size(display_width, display_height)
        camera_fps = float(
            self.camera.get(
                "fps",
                self.engine_settings.get(
                    "fps",
                    self.infer.get("stream_fps", self.infer.get("process_fps", self.infer.get("camera_read_fps", 20))),
                ),
            )
            or 20
        )
        record_fps = float(
            self.infer.get("record_fps", self.engine_settings.get("record_fps", self.camera.get("fps_record", camera_fps)))
            or 20
        )
        bitrate_coefficient = _sanitize_bitrate_coefficient(
            self.infer.get("bitrate_coefficient", self.engine_settings.get("bitrate_coefficient", 1.0)),
            1.0,
        )

        settings = {
            "camera_index": int(self.camera.get("camera_index", self.engine_settings.get("camera_index", 0)) or 0),
            "display_chn": int(
                self.camera.get(
                    "display_chn",
                    self.engine_settings.get("display_chn", self.camera.get("camera_chn", 2)),
                )
                or 2
            ),
            "infer_chn": int(
                self.camera.get(
                    "infer_chn",
                    self.engine_settings.get("infer_chn", self.camera.get("camera_chn", 2)),
                )
                or 2
            ),
            "sensor_scale": sensor_scale,
            "display_width": display_width,
            "display_height": display_height,
            "infer_width": infer_width,
            "infer_height": infer_height,
            "classes_num": int(self.infer.get("classes_num", self.engine_settings.get("classes_num", len(label_names) or 80)) or 80),
            "reg": int(self.infer.get("reg", self.engine_settings.get("reg", self.camera.get("reg", 16))) or 16),
            "conf_thres": default_conf_thres,
            "detector_conf_thres": detector_conf_thres,
            "iou_thres": float(
                self.infer.get("iou_thres", self.engine_settings.get("iou_thres", self.camera.get("iou_thres", 0.45)))
                or 0.45
            ),
            "stream_fps": camera_fps,
            "process_fps": camera_fps,
            "camera_read_fps": camera_fps,
            "record_fps": record_fps,
            "bitrate_coefficient": bitrate_coefficient,
            "record_enabled": bool(self.infer.get("record_enabled", self.engine_settings.get("record_enabled", True))),
            "record_original": bool(self.infer.get("record_original", self.engine_settings.get("record_original", True))),
            "record_labeled": bool(self.infer.get("record_labeled", self.engine_settings.get("record_labeled", True))),
            "record_segment_seconds": int(
                self.infer.get("record_segment_seconds", self.engine_settings.get("record_segment_seconds", self.infer.get("segment_seconds", 60)))
                or 60
            ),
            "record_encode_type": int(
                self.infer.get("record_encode_type", self.engine_settings.get("record_encode_type", 2)) or 2
            ),
            "record_queue_size": int(
                self.infer.get("record_queue_size", self.engine_settings.get("record_queue_size", 32)) or 32
            ),
            "record_original_video_chn": int(
                self.infer.get("record_original_video_chn", self.engine_settings.get("record_original_video_chn", 0)) or 0
            ),
            "record_labeled_video_chn": int(
                self.infer.get("record_labeled_video_chn", self.engine_settings.get("record_labeled_video_chn", 1)) or 1
            ),
            "stream_width": int(
                self.infer.get("stream_width", self.engine_settings.get("stream_width", default_stream_width))
                or default_stream_width
            ),
            "stream_height": int(
                self.infer.get("stream_height", self.engine_settings.get("stream_height", default_stream_height))
                or default_stream_height
            ),
            "jpeg_quality": int(self.infer.get("jpeg_quality", self.engine_settings.get("jpeg_quality", 70)) or 70),
            "cleanup_retention_hours": int(
                self.infer.get("cleanup_retention_hours", self.engine_settings.get("cleanup_retention_hours", 3)) or 3
            ),
            "cleanup_interval_sec": int(
                self.infer.get("cleanup_interval_sec", self.engine_settings.get("cleanup_interval_sec", 300)) or 300
            ),
            "auto_split_timeout_sec": int(
                self.infer.get("auto_split_timeout_sec", self.engine_settings.get("auto_split_timeout_sec", 3 * 60 * 60))
                or 3 * 60 * 60
            ),
            "auto_split_check_interval_sec": int(
                self.infer.get(
                    "auto_split_check_interval_sec",
                    self.engine_settings.get("auto_split_check_interval_sec", 10 * 60),
                )
                or 10 * 60
            ),
            "label_names": list(label_names),
            "labels_confs": labels_confs,
            "brightness_delta": _coerce_int(
                self.camera.get("brightness_delta", self.engine_settings.get("brightness_delta"))
            )
            or 0,
        }
        settings["record_original_bitrate_kbps"] = _estimate_encoder_bitrate_kbps(
            display_width,
            display_height,
            record_fps,
            bitrate_coefficient,
        )
        settings["record_labeled_bitrate_kbps"] = _estimate_encoder_bitrate_kbps(
            display_width,
            display_height,
            camera_fps,
            bitrate_coefficient * LABELED_VIDEO_BITRATE_FACTOR,
        )
        return settings

    def resolve_model_path(self):
        model_path_value = self.infer.get("model_path", self.engine_settings.get("model_path", "best.bin"))
        model_path = Path(str(model_path_value))
        if not model_path.is_absolute():
            model_path = (self.config_path.parent / model_path).resolve()
        return model_path


class RDKInferenceService:
    def __init__(self, runtime_config: RuntimeConfig):
        self.config = runtime_config
        self.shutdown_event = threading.Event()
        self._state_lock = threading.Lock()
        self._status = {
            "engine": "rdk",
            "running": False,
            "last_frame_ts": 0.0,
            "fps": 0.0,
            "frames": 0,
            "detections": 0,
            "last_error": "",
            "mjpeg_clients": 0,
            "video_width": 0,
            "video_height": 0,
        }

        settings = self.config.runtime_settings
        infer_cfg = self.config.infer
        label_names = settings.get("label_names") or infer_cfg.get("label_names") or list(infer_cfg.get("labels_confs", {}).keys())
        self.label_names = list(label_names)
        self.labels_confs = _normalize_labels_confs(settings.get("labels_confs") or infer_cfg.get("labels_confs"))
        self.default_label_conf_thres = _normalize_probability(settings.get("conf_thres", 0.25), 0.25)
        self.detector_conf_thres = _sanitize_detector_conf_threshold(
            settings.get("detector_conf_thres", self.default_label_conf_thres),
            self.default_label_conf_thres,
        )
        self.dev_flag = bool(infer_cfg.get("dev_flag", True))
        self.api_port = int(infer_cfg.get("api_port", 5050))

        sensor_scale = _coerce_positive_float(settings.get("sensor_scale")) or _resolve_sensor_scale(
            self.config.camera,
            infer_cfg,
            self.config.engine_settings,
        )
        display_width = _coerce_positive_int(settings.get("display_width"))
        display_height = _coerce_positive_int(settings.get("display_height"))
        if display_width is None or display_height is None:
            display_width, display_height = _resolve_display_dimensions(sensor_scale)
        infer_width = int(settings.get("infer_width", self.config.camera.get("infer_width", 640)) or 640)
        infer_height = int(settings.get("infer_height", self.config.camera.get("infer_height", 640)) or 640)

        self.camera = MipiCamera(
            MipiCameraConfig(
                camera_index=int(settings.get("camera_index", self.config.camera.get("camera_index", 0)) or 0),
                display_chn=int(settings.get("display_chn", self.config.camera.get("display_chn", self.config.camera.get("camera_chn", 2)) or 2)),
                infer_chn=int(settings.get("infer_chn", self.config.camera.get("infer_chn", self.config.camera.get("camera_chn", 2)) or 2)),
                display_width=display_width,
                display_height=display_height,
                infer_width=infer_width,
                infer_height=infer_height,
                brightness_delta=_coerce_int(settings.get("brightness_delta")) or 0,
            )
        )

        self.detector = None
        if self.dev_flag:
            model_path = self.config.resolve_model_path()
            self.detector = YOLODetector(
                model_file=str(model_path),
                classes_num=int(settings.get("classes_num", len(self.label_names) or 80) or 80),
                reg=int(settings.get("reg", 16) or 16),
                conf=self.detector_conf_thres,
                iou=float(settings.get("iou_thres", 0.45) or 0.45),
            )
            logger.info("[DNN] using model: %s", model_path)
            logger.info(
                "[DNN] label conf thresholds loaded=%d detector_conf_thres=%.3f default_label_conf_thres=%.3f",
                len(self.labels_confs),
                self.detector_conf_thres,
                self.default_label_conf_thres,
            )
            if self.label_names and len(self.label_names) != self.detector.classes_num:
                logger.warning(
                    "[DNN] label_names count mismatch: configured=%d, model classes=%d. "
                    "Current label_names=%s",
                    len(self.label_names),
                    self.detector.classes_num,
                    self.label_names,
                )

        self.process_interval_sec = 0.0 if float(settings.get("process_fps", 0)) <= 0 else 1.0 / float(settings.get("process_fps", 20))
        self.stream_interval_sec = 0.0 if float(settings.get("stream_fps", 0)) <= 0 else 1.0 / float(settings.get("stream_fps", 20))
        self.record_interval_sec = 0.0 if float(settings.get("record_fps", 0)) <= 0 else 1.0 / float(settings.get("record_fps", 20))
        stream_fps_candidates = [
            float(settings.get("process_fps", 0) or 0),
            float(settings.get("stream_fps", 0) or 0),
        ]
        stream_fps_candidates = [fps for fps in stream_fps_candidates if fps > 0]
        self.max_stream_fps = max(1, int(round(min(stream_fps_candidates)))) if stream_fps_candidates else 20
        self.default_stream_fps = self.max_stream_fps
        self._next_stream_ts = 0.0
        self._next_record_ts = 0.0

        self.video_width = display_width
        self.video_height = display_height
        default_stream_width, default_stream_height = _half_frame_size(self.video_width, self.video_height)
        self.stream_frame_width = _coerce_positive_int(settings.get("stream_width")) or default_stream_width
        self.stream_frame_height = _coerce_positive_int(settings.get("stream_height")) or default_stream_height
        self.disappear_line = 0
        self.last_split_at = time.time()
        self.last_split_files = {"original": None, "labeled": None}

        upload_root = Path("./upload")
        self.original_dir = upload_root / "original"
        self.label_dir = upload_root / "label"

        record_enabled = bool(settings.get("record_enabled", True))
        self.original_writer = None
        if record_enabled and bool(settings.get("record_original", True)):
            self.original_writer = HardwareSegmentedRecorder(
                HardwareRecorderConfig(
                    output_dir=self.original_dir,
                    prefix="video",
                    width=display_width,
                    height=display_height,
                    fps=float(settings.get("record_fps", settings.get("process_fps", 20)) or 20),
                    segment_seconds=int(settings.get("record_segment_seconds", infer_cfg.get("segment_seconds", 60)) or 60),
                    encode_type=int(settings.get("record_encode_type", 2) or 2),
                    bitrate_kbps=int(settings.get("record_original_bitrate_kbps")),
                    video_chn=int(settings.get("record_original_video_chn", 0) or 0),
                    queue_size=int(settings.get("record_queue_size", 32) or 32),
                )
            )

        self.labeled_writer = None
        if record_enabled and self.dev_flag and bool(settings.get("record_labeled", True)):
            self.labeled_writer = HardwareSegmentedRecorder(
                HardwareRecorderConfig(
                    output_dir=self.label_dir,
                    prefix="label",
                    width=display_width,
                    height=display_height,
                    fps=float(settings.get("process_fps", 20) or 20),
                    segment_seconds=int(settings.get("record_segment_seconds", infer_cfg.get("segment_seconds", 60)) or 60),
                    encode_type=int(settings.get("record_encode_type", 2) or 2),
                    bitrate_kbps=int(settings.get("record_labeled_bitrate_kbps")),
                    video_chn=int(settings.get("record_labeled_video_chn", 1) or 1),
                    queue_size=int(settings.get("record_queue_size", 32) or 32),
                )
            )

        self._jpeg_quality = max(30, min(int(settings.get("jpeg_quality", 70) or 70), 95))
        self._annotated_queue = queue.Queue(maxsize=1)
        self._annotated_lock = threading.Lock()
        self._latest_annotated = None
        self._jpeg_cond = threading.Condition()
        self._latest_jpeg = None
        self._jpeg_seq = 0
        self._mjpeg_clients = 0
        self._mjpeg_clients_lock = threading.Lock()
        self.jpeg_thread = None

        self.labels_publisher = LabelsPublisher(endpoint=f"ipc:///tmp/{self.api_port}.ipc", conflate=False, verbose=False)

        self.cleanup_retention_hours = int(settings.get("cleanup_retention_hours", 3) or 3)
        self.cleanup_interval_sec = int(settings.get("cleanup_interval_sec", 300) or 300)
        self.auto_split_timeout_sec = int(settings.get("auto_split_timeout_sec", 3 * 60 * 60) or 3 * 60 * 60)
        self.auto_split_check_interval_sec = int(settings.get("auto_split_check_interval_sec", 10 * 60) or 10 * 60)
        self.http_thread = None
        self.cleanup_thread = None
        self.auto_split_thread = None
        self._fps_count = 0
        self._fps_started_at = time.time()
        self.perf_log_interval_sec = float(settings.get("perf_log_interval_sec", 5.0) or 5.0)
        self._perf_reset()

    def _perf_reset(self):
        self._perf_started_at = time.monotonic()
        self._perf_stats = {
            "loops": 0,
            "display_reads": 0,
            "infer_reads": 0,
            "display_read_sec": 0.0,
            "infer_read_sec": 0.0,
            "read_none": 0,
            "convert_sec": 0.0,
            "infer_sec": 0.0,
            "draw_sec": 0.0,
            "publish_sec": 0.0,
            "record_sec": 0.0,
            "record_original_writes": 0,
            "record_labeled_writes": 0,
            "stream_sec": 0.0,
            "stream_packets": 0,
            "loop_sec": 0.0,
            "errors": 0,
        }

    def _get_label_name(self, class_id: int) -> str:
        return self.label_names[class_id] if 0 <= class_id < len(self.label_names) else f"cls_{class_id}"

    def _get_label_conf_threshold(self, class_id: int) -> float:
        label_name = self._get_label_name(class_id)
        return float(self.labels_confs.get(label_name, self.default_label_conf_thres))

    def _passes_label_conf_threshold(self, class_id: int, score: float) -> bool:
        return float(score) >= self._get_label_conf_threshold(class_id)

    def _maybe_log_perf(self, force: bool = False):
        elapsed = time.monotonic() - self._perf_started_at
        if not force and elapsed < self.perf_log_interval_sec:
            return

        stats = self._perf_stats
        loops = max(int(stats["loops"]), 1)
        logger.info(
            "[PERF] window=%.2fs loops=%d loop_fps=%.2f original_record_fps=%.2f labeled_record_fps=%.2f stream_pps=%.2f "
            "read_display_ms=%.1f read_infer_ms=%.1f convert_ms=%.1f infer_ms=%.1f draw_ms=%.1f "
            "publish_ms=%.1f record_ms=%.1f stream_ms=%.1f none_reads=%d errors=%d mjpeg_clients=%d",
            elapsed,
            int(stats["loops"]),
            stats["loops"] / elapsed if elapsed > 0 else 0.0,
            stats["record_original_writes"] / elapsed if elapsed > 0 else 0.0,
            stats["record_labeled_writes"] / elapsed if elapsed > 0 else 0.0,
            stats["stream_packets"] / elapsed if elapsed > 0 else 0.0,
            1000.0 * stats["display_read_sec"] / max(int(stats["display_reads"]), 1),
            1000.0 * stats["infer_read_sec"] / max(int(stats["infer_reads"]), 1),
            1000.0 * stats["convert_sec"] / loops,
            1000.0 * stats["infer_sec"] / loops,
            1000.0 * stats["draw_sec"] / loops,
            1000.0 * stats["publish_sec"] / loops,
            1000.0 * stats["record_sec"] / loops,
            1000.0 * stats["stream_sec"] / loops,
            int(stats["read_none"]),
            int(stats["errors"]),
            self.mjpeg_client_count(),
        )
        self._perf_reset()

    def _update_status(self, **updates):
        with self._state_lock:
            self._status.update(updates)
            self._status["mjpeg_clients"] = self.mjpeg_client_count()
            self._status["video_width"] = self.video_width
            self._status["video_height"] = self.video_height

    def get_status(self):
        with self._state_lock:
            return dict(self._status)

    def set_disappear_line(self, line_x: int):
        try:
            line_x = int(line_x)
        except Exception:
            line_x = 0

        if line_x < 0:
            line_x = 0
        if self.video_width > 0 and line_x > self.video_width:
            line_x = self.video_width

        self.disappear_line = line_x
        logger.info("[LINE] set disappear_line=%d", self.disappear_line)
        return self.disappear_line

    def split_recordings(self):
        original_name = self.original_writer.split_now() if self.original_writer else None
        labeled_name = self.labeled_writer.split_now() if self.labeled_writer else None
        self.last_split_at = time.time()
        self.last_split_files = {"original": original_name, "labeled": labeled_name}
        logger.info("[SPLIT] original=%s labeled=%s", original_name, labeled_name)
        return dict(self.last_split_files)

    def _should_emit_original_record(self):
        if self.record_interval_sec <= 0:
            return True
        if self.process_interval_sec > 0 and self.record_interval_sec <= (self.process_interval_sec * 1.05):
            return True
        now = time.monotonic()
        if self._next_record_ts <= 0.0:
            self._next_record_ts = now
        if now >= self._next_record_ts:
            self._next_record_ts = max(self._next_record_ts + self.record_interval_sec, now)
            return True
        return False

    def _should_emit_stream(self):
        if self.stream_interval_sec <= 0:
            return True
        if self.process_interval_sec > 0 and self.stream_interval_sec <= (self.process_interval_sec * 1.05):
            return True
        now = time.monotonic()
        if self._next_stream_ts <= 0.0:
            self._next_stream_ts = now
        if now >= self._next_stream_ts:
            self._next_stream_ts = max(self._next_stream_ts + self.stream_interval_sec, now)
            return True
        return False

    def _tick_fps(self):
        self._fps_count += 1
        if self._fps_count < 30:
            return self.get_status().get("fps", 0.0)
        now = time.time()
        elapsed = now - self._fps_started_at
        fps = self._fps_count / elapsed if elapsed > 0 else 0.0
        self._fps_count = 0
        self._fps_started_at = now
        self._update_status(fps=fps)
        return fps

    def _draw_overlay(self, frame: np.ndarray):
        if self.disappear_line > 0:
            cv2.line(frame, (self.disappear_line, 0), (self.disappear_line, self.video_height), (0, 0, 255), 2)

        cv2.putText(
            frame,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            (10, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

        fps = self.get_status().get("fps", 0.0)
        cv2.putText(
            frame,
            f"FPS: {fps:.1f}",
            (10, 58),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 0),
            2,
            cv2.LINE_AA,
        )

    def _put_latest_annotated(self, frame: np.ndarray):
        with self._annotated_lock:
            self._latest_annotated = frame

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

    def mjpeg_client_count(self) -> int:
        with self._mjpeg_clients_lock:
            return self._mjpeg_clients

    def _change_mjpeg_clients(self, delta: int):
        with self._mjpeg_clients_lock:
            self._mjpeg_clients = max(0, self._mjpeg_clients + delta)

    def _jpeg_loop(self):
        while not self.shutdown_event.is_set():
            try:
                try:
                    item = self._annotated_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if item is None:
                    break

                quality = self._jpeg_quality
                ok, encoded = cv2.imencode(".jpg", item, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if not ok:
                    continue

                with self._jpeg_cond:
                    self._latest_jpeg = encoded.tobytes()
                    self._jpeg_seq += 1
                    self._jpeg_cond.notify_all()
            except Exception as exc:
                logger.exception("[MJPEG] jpeg loop failed")
                self._update_status(last_error=str(exc))
                time.sleep(0.05)

    def next_jpeg(self, last_seq: int | None = None, timeout: float = 2.0):
        deadline = time.monotonic() + max(timeout, 0.1)
        with self._jpeg_cond:
            while not self.shutdown_event.is_set():
                if self._latest_jpeg is not None and (last_seq is None or self._jpeg_seq != last_seq):
                    return self._latest_jpeg, self._jpeg_seq

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("no jpeg frame available yet")
                self._jpeg_cond.wait(timeout=remaining)

        raise RuntimeError("service stopping")

    def _prepare_stream_frame(self, frame: np.ndarray):
        if frame is None or frame.size == 0:
            return None
        if frame.shape[1] == self.stream_frame_width and frame.shape[0] == self.stream_frame_height:
            return frame
        interpolation = cv2.INTER_AREA
        if frame.shape[1] < self.stream_frame_width or frame.shape[0] < self.stream_frame_height:
            interpolation = cv2.INTER_LINEAR
        return cv2.resize(
            frame,
            (self.stream_frame_width, self.stream_frame_height),
            interpolation=interpolation,
        )

    def _cleanup_worker(self):
        while not self.shutdown_event.wait(self.cleanup_interval_sec):
            try:
                cutoff = time.time() - self.cleanup_retention_hours * 3600
                deleted = 0
                for folder in (self.original_dir, self.label_dir):
                    if not folder.exists():
                        continue
                    for pattern in ("*.mp4", "*.h264", "*.h265", "*.mjpg"):
                        for file_path in folder.glob(pattern):
                            if not file_path.is_file():
                                continue
                            try:
                                timestamp = int(file_path.stem.split("_", 1)[1])
                            except Exception:
                                continue
                            if timestamp < cutoff:
                                file_path.unlink()
                                deleted += 1
                if deleted:
                    logger.info("[CLEANUP] deleted %d expired mp4 files", deleted)
            except Exception as exc:
                logger.warning("[CLEANUP] failed: %s", exc)

    def _auto_split_worker(self):
        while not self.shutdown_event.wait(self.auto_split_check_interval_sec):
            try:
                if time.time() - self.last_split_at >= self.auto_split_timeout_sec:
                    logger.warning("[AUTO_SPLIT] timeout reached, rotating recordings")
                    self.split_recordings()
            except Exception as exc:
                logger.warning("[AUTO_SPLIT] failed: %s", exc)

    def _create_http_app(self):
        app = Flask(__name__)
        CORS(app)

        @app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok", "engine": "rdk"}), 200

        @app.route("/status", methods=["GET"])
        def status():
            payload = self.get_status()
            payload.update(
                {
                    "status": "ok" if payload.get("running") else "starting",
                    "last_split_files": dict(self.last_split_files),
                }
            )
            return jsonify(payload), 200

        @app.route("/split", methods=["POST", "GET"])
        def split():
            files = self.split_recordings()
            return jsonify({"success": True, "files": files, "message": "视频截断成功"}), 200

        @app.route("/stream", methods=["GET"])
        def mjpeg_stream():
            try:
                requested_fps = int(request.args.get("fps", self.default_stream_fps) or self.default_stream_fps)
            except Exception:
                requested_fps = self.default_stream_fps
            fps = max(1, min(requested_fps, self.max_stream_fps, 60))
            try:
                quality = max(30, min(int(request.args.get("quality", self._jpeg_quality) or self._jpeg_quality), 95))
            except Exception:
                quality = self._jpeg_quality
            self._jpeg_quality = quality

            @stream_with_context
            def generate():
                self._change_mjpeg_clients(1)
                last_seq = -1
                interval = 1.0 / fps
                timeout = max(interval * 3.0, 1.0)
                try:
                    while not self.shutdown_event.is_set():
                        loop_started_at = time.monotonic()
                        try:
                            jpg, last_seq = self.next_jpeg(
                                last_seq=last_seq,
                                timeout=timeout,
                            )
                            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                        except GeneratorExit:
                            raise
                        except Exception:
                            time.sleep(0.05)

                        elapsed = time.monotonic() - loop_started_at
                        remaining = interval - elapsed
                        if remaining > 0:
                            time.sleep(remaining)
                finally:
                    self._change_mjpeg_clients(-1)

            headers = {
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            }
            return Response(
                generate(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
                headers=headers,
            )

        @app.route("/disappear_line", methods=["POST", "GET"])
        def disappear_line():
            if request.method == "POST":
                payload = request.get_json() or {}
                line_x = self.set_disappear_line(payload.get("x", 0))
                return jsonify({"success": True, "x": line_x}), 200

            return jsonify(
                {
                    "success": True,
                    "x": self.disappear_line,
                    "width": self.video_width,
                    "height": self.video_height,
                }
            ), 200

        return app

    def _start_http_server(self):
        app = self._create_http_app()

        def _worker():
            logger.info("[HTTP] listening at http://0.0.0.0:%d", self.api_port)
            app.run(host="0.0.0.0", port=self.api_port, debug=False, use_reloader=False, threaded=True)

        self.http_thread = threading.Thread(target=_worker, daemon=True, name="rdk-x5-http")
        self.http_thread.start()

    def start(self):
        self.original_dir.mkdir(parents=True, exist_ok=True)
        self.label_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "[CONFIG] sensor=%dx%d infer=%dx%d stream=%dx%d dev=%s process_fps=%.2f record_fps=%.2f stream_fps=%.2f "
            "camera_read_fps=%.2f record_encode_type=%d bitrate_coef=%.3f original_bitrate=%d labeled_bitrate=%d "
            "record_queue=%d record_original_chn=%d record_labeled_chn=%d "
            "note=camera_read_fps is not enforced by open_cam in current implementation",
            self.video_width,
            self.video_height,
            self.camera.config.infer_width,
            self.camera.config.infer_height,
            self.stream_frame_width,
            self.stream_frame_height,
            self.dev_flag,
            0.0 if self.process_interval_sec <= 0 else (1.0 / self.process_interval_sec),
            0.0 if self.record_interval_sec <= 0 else (1.0 / self.record_interval_sec),
            0.0 if self.stream_interval_sec <= 0 else (1.0 / self.stream_interval_sec),
            float(self.config.runtime_settings.get("camera_read_fps", 0) or 0),
            int(self.config.runtime_settings.get("record_encode_type", 2) or 2),
            float(self.config.runtime_settings.get("bitrate_coefficient", 1.0) or 1.0),
            int(self.config.runtime_settings.get("record_original_bitrate_kbps")),
            int(self.config.runtime_settings.get("record_labeled_bitrate_kbps")),
            int(self.config.runtime_settings.get("record_queue_size", 32) or 32),
            int(self.config.runtime_settings.get("record_original_video_chn", 0) or 0),
            int(self.config.runtime_settings.get("record_labeled_video_chn", 1) or 1),
        )

        self.camera.open()
        self.jpeg_thread = threading.Thread(target=self._jpeg_loop, daemon=True, name="rdk-x5-mjpeg")
        self.jpeg_thread.start()
        self._start_http_server()

        if self.original_writer is not None:
            self.original_writer.start()
        if self.labeled_writer is not None:
            self.labeled_writer.start()

        self.cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True, name="rdk-x5-cleanup")
        self.cleanup_thread.start()

        self.auto_split_thread = threading.Thread(target=self._auto_split_worker, daemon=True, name="rdk-x5-auto-split")
        self.auto_split_thread.start()

        self.last_split_at = time.time()
        self._update_status(running=True, last_error="")
        logger.info("[SERVICE] started, mjpeg=http://0.0.0.0:%d/stream", self.api_port)

    def stop(self):
        self.shutdown_event.set()
        self._update_status(running=False)

        try:
            self.labels_publisher.close()
        except Exception:
            pass

        try:
            self._annotated_queue.put_nowait(None)
        except queue.Full:
            pass

        try:
            if self.jpeg_thread is not None:
                self.jpeg_thread.join(timeout=1.0)
                self.jpeg_thread = None
        except Exception:
            pass

        try:
            if self.original_writer is not None:
                self.original_writer.stop()
            if self.labeled_writer is not None:
                self.labeled_writer.stop()
        except Exception:
            pass

        try:
            self.camera.close()
        except Exception:
            pass

        self._maybe_log_perf(force=True)
        logger.info("[SERVICE] stopped")

    def run(self):
        next_ts = time.monotonic()
        while not self.shutdown_event.is_set():
            try:
                loop_started_at = time.monotonic()
                self._perf_stats["loops"] += 1
                if self.process_interval_sec > 0:
                    now = time.monotonic()
                    if now < next_ts:
                        time.sleep(next_ts - now)
                    next_ts = max(next_ts + self.process_interval_sec, time.monotonic())

                read_started_at = time.monotonic()
                display_nv12 = self.camera.read_display_nv12()
                self._perf_stats["display_reads"] += 1
                self._perf_stats["display_read_sec"] += time.monotonic() - read_started_at
                read_started_at = time.monotonic()
                infer_nv12 = self.camera.read_infer_nv12()
                self._perf_stats["infer_reads"] += 1
                self._perf_stats["infer_read_sec"] += time.monotonic() - read_started_at
                if display_nv12 is None or infer_nv12 is None:
                    self._perf_stats["read_none"] += 1
                    self._perf_stats["loop_sec"] += time.monotonic() - loop_started_at
                    self._maybe_log_perf()
                    time.sleep(0.01)
                    continue

                convert_started_at = time.monotonic()
                original_frame = self.camera.nv12_to_bgr(display_nv12, self.video_width, self.video_height)
                self._perf_stats["convert_sec"] += time.monotonic() - convert_started_at
                annotated_frame = original_frame.copy()
                labels_for_frame = []

                if self.dev_flag and self.detector is not None:
                    infer_started_at = time.monotonic()
                    ids, scores, bboxes = self.detector.infer_nv12(
                        infer_nv12,
                        width=self.camera.config.infer_width,
                        height=self.camera.config.infer_height,
                    )
                    self._perf_stats["infer_sec"] += time.monotonic() - infer_started_at
                    scale_x = self.video_width / self.camera.config.infer_width
                    scale_y = self.video_height / self.camera.config.infer_height
                    for class_id, score, bbox in zip(ids, scores, bboxes):
                        cid = int(class_id)
                        score_value = float(score)
                        if not self._passes_label_conf_threshold(cid, score_value):
                            continue
                        x1, y1, x2, y2 = [int(v) for v in bbox]
                        mapped = (
                            int(x1 * scale_x),
                            int(y1 * scale_y),
                            int(x2 * scale_x),
                            int(y2 * scale_y),
                        )
                        labels_for_frame.append([mapped[0], mapped[1], mapped[2], mapped[3], score_value, cid])
                        draw_detection(annotated_frame, mapped, score_value, cid, self.label_names)

                draw_started_at = time.monotonic()
                self._draw_overlay(annotated_frame)
                self._perf_stats["draw_sec"] += time.monotonic() - draw_started_at

                if self.dev_flag:
                    publish_started_at = time.monotonic()
                    self.labels_publisher.publish(labels_for_frame)
                    self._perf_stats["publish_sec"] += time.monotonic() - publish_started_at

                record_started_at = None
                if self.original_writer is not None and self._should_emit_original_record():
                    record_started_at = time.monotonic()
                    self.original_writer.write_nv12(display_nv12)
                    self._perf_stats["record_original_writes"] += 1

                if self.labeled_writer is not None:
                    if record_started_at is None:
                        record_started_at = time.monotonic()
                    self.labeled_writer.write_bgr(annotated_frame)
                    self._perf_stats["record_labeled_writes"] += 1

                if record_started_at is not None:
                    self._perf_stats["record_sec"] += time.monotonic() - record_started_at

                if self._should_emit_stream() and self.mjpeg_client_count() > 0:
                    stream_started_at = time.monotonic()
                    stream_frame = self._prepare_stream_frame(annotated_frame)
                    if stream_frame is not None:
                        self._put_latest_annotated(stream_frame)
                        self._perf_stats["stream_packets"] += 1
                    self._perf_stats["stream_sec"] += time.monotonic() - stream_started_at

                fps = self._tick_fps()
                self._update_status(
                    last_frame_ts=time.time(),
                    frames=int(self.get_status().get("frames", 0)) + 1,
                    detections=len(labels_for_frame),
                    fps=fps,
                )
                self._perf_stats["loop_sec"] += time.monotonic() - loop_started_at
                self._maybe_log_perf()
            except Exception as exc:
                logger.exception("[SERVICE] frame loop failed")
                self._perf_stats["errors"] += 1
                self._update_status(last_error=str(exc))
                self._perf_stats["loop_sec"] += time.monotonic() - loop_started_at
                self._maybe_log_perf()
                time.sleep(0.05)


def main(argv=None):
    parser = argparse.ArgumentParser(description="RDK X5 inference backend service")
    parser.add_argument("--name", default="", help="workstation name, only for process compatibility")
    parser.add_argument("--config", default="config.json", help="runtime config path")
    args = parser.parse_args(argv)

    runtime_config = RuntimeConfig(Path(args.config))
    service = RDKInferenceService(runtime_config)

    def _shutdown(*_args):
        service.shutdown_event.set()

    for sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            signal_value = getattr(signal, sig)
            signal.signal(signal_value, _shutdown)

    service.start()
    try:
        service.run()
        return 0
    finally:
        service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
