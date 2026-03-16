from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import time
import os
import shutil
import numpy as np
import threading
try:
    from hobot_vio import libsrcampy as srcampy
except Exception:  # pragma: no cover
    srcampy = None

logger = logging.getLogger("RDK_CAMERA")


def crop_nv12_roi(nv12_bytes, width: int, height: int,
                  x: int, y: int, w: int, h: int) -> bytes:
    """
    从 NV12 原始字节中裁剪 ROI，返回裁剪后的 NV12 bytes
    要求 x/y/w/h 最好为偶数，否则会自动向下/向内对齐
    """

    # ---- 1. 边界限制 ----
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(2, min(w, width - x))
    h = max(2, min(h, height - y))

    # ---- 2. 对齐到偶数，保证 UV 正常 ----
    x &= ~1
    y &= ~1
    w &= ~1
    h &= ~1

    if x + w > width:
        w = (width - x) & ~1
    if y + h > height:
        h = (height - y) & ~1

    # ---- 3. 零拷贝视图 ----
    nv12 = np.frombuffer(nv12_bytes, dtype=np.uint8)

    y_size = width * height
    uv_size = width * height // 2

    if nv12.size < y_size + uv_size:
        raise ValueError("nv12_bytes 长度不足")

    y_plane = nv12[:y_size].reshape(height, width)
    uv_plane = nv12[y_size:y_size + uv_size].reshape(height // 2, width)

    # ---- 4. 裁剪 Y ----
    y_roi = y_plane[y:y + h, x:x + w]

    # ---- 5. 裁剪 UV ----
    uv_y = y // 2
    uv_h = h // 2
    uv_roi = uv_plane[uv_y:uv_y + uv_h, x:x + w]

    # ---- 6. 拼回 NV12 bytes ----
    # 注意：这里 tobytes() 会产生一次必要拷贝，作为输出
    return y_roi.tobytes() + uv_roi.tobytes()


def nv12_roi_to_bgr(nv12_bytes, width: int, height: int,
                    x: int, y: int, w: int, h: int) -> np.ndarray:
    """
    直接从 NV12 bytes 中裁 ROI 并转成 BGR
    比整图转 BGR 再裁剪更省 CPU / 内存带宽
    """
    roi_nv12_bytes = crop_nv12_roi(nv12_bytes, width, height, x, y, w, h)

    # 对齐后的实际尺寸
    x2 = x & ~1
    y2 = y & ~1
    w2 = min(w, width - x2) & ~1
    h2 = min(h, height - y2) & ~1

    roi_nv12 = np.frombuffer(roi_nv12_bytes, dtype=np.uint8).reshape((h2 * 3 // 2, w2))
    return cv2.cvtColor(roi_nv12, cv2.COLOR_YUV2BGR_NV12)


def nv12_resize(nv12_bytes, src_w, src_h, dst_w, dst_h):
    arr = np.frombuffer(nv12_bytes, dtype=np.uint8)

    y_size = src_w * src_h
    uv_size = src_w * src_h // 2

    if arr.size != y_size + uv_size:
        raise ValueError("NV12 size error")

    # ---- 拆分平面 ----
    y_plane = arr[:y_size].reshape(src_h, src_w)
    uv_plane = arr[y_size:].reshape(src_h // 2, src_w)

    # ---- resize Y ----
    y_resized = cv2.resize(y_plane, (dst_w, dst_h), interpolation=cv2.INTER_LINEAR)

    # ---- resize UV ----
    uv_resized = cv2.resize(
        uv_plane,
        (dst_w, dst_h // 2),
        interpolation=cv2.INTER_LINEAR
    )

    # ---- 拼回 NV12 ----
    out = np.empty(dst_w * dst_h * 3 // 2, dtype=np.uint8)

    out[:dst_w * dst_h] = y_resized.flatten()
    out[dst_w * dst_h:] = uv_resized.flatten()

    return out.tobytes()


def nv12_adjust_brightness(nv12_bytes, width, height, delta):
    """
    delta > 0 变亮
    delta < 0 变暗
    """

    arr = np.frombuffer(nv12_bytes, dtype=np.uint8)

    y_size = width * height

    # Y plane
    y = arr[:y_size]

    # 调整亮度
    y = np.clip(y.astype(np.int16) + delta, 0, 255).astype(np.uint8)

    # 拼回 NV12
    out = np.empty_like(arr)
    out[:y_size] = y
    out[y_size:] = arr[y_size:]

    return out.tobytes()

@dataclass(frozen=True)
class CameraConfig:
    camera_index: int
    sensor_width: int
    sensor_height: int
    infer_width: int
    infer_height: int
    stream_fps: int
    grab_x: int
    grab_y: int
    grab_w: int
    grab_h: int
    brightness_delta: int

class MipiCamera:
    def __init__(self, config: CameraConfig) -> None:
        if srcampy is None:
            raise RuntimeError("hobot_vio.libsrcampy is unavailable. Run this service on an RDK device.")

        self.config = config
        self._cam = srcampy.Camera()
        self._opened = False

    def open(self) -> None:
        if self._opened:
            return

        cfg = self.config
        ret = None
        try:
            # Preferred signature from official Python multimedia docs.
            # ret = self._cam.open_cam(
            #     cfg.camera_index,
            #     -1,
            #     -1,
            #     [640, 640],
            #     [480, 480],
            #     1080,
            #     1920,
            # )
            ret = self._cam.open_cam(
                cfg.camera_index,
                -1,
                -1,
                [self.config.infer_width, self.config.sensor_width],
                [self.config.infer_height, self.config.sensor_height],
                1080,
                1920,
            )
        except TypeError:
            # Fallback for BSP variants with extended open_cam signature.
            # ret = self._cam.open_cam(
            #     cfg.camera_index,
            #     -1,
            #     -1,
            #     [cfg.sensor_width, cfg.infer_width],
            #     [cfg.sensor_height, cfg.infer_height],
            #     cfg.sensor_height,
            #     cfg.sensor_width,
            # )
            logger.error("open_cam failed: %s", "not implemented")
            raise RuntimeError("open_cam failed: not implemented")
        if ret is not None and ret != 0:
            raise RuntimeError(
                "open_cam failed: "
                f"ret={ret}, camera_index={cfg.camera_index}, display={cfg.sensor_width}x{cfg.sensor_height}, "
                f"infer={cfg.infer_width}x{cfg.infer_height}. "
                "Check sensor connection and camera index."
            )
        self._opened = True
        

    def close(self) -> None:
        if not self._opened:
            return
        self._cam.close_cam()
        self._opened = False
        logger.info("camera closed")

    def read_infer_nv12(self) -> np.ndarray | None:
        nv12_bytes = self.read_nv12(2)
        nv12_bytes = nv12_resize(nv12_bytes, self.config.grab_w, self.config.grab_h, self.config.infer_width, self.config.infer_height)
        if nv12_bytes is None:
            return None
        return np.frombuffer(nv12_bytes, dtype=np.uint8)


    def read_display_nv12(self) -> np.ndarray | None:
        nv12_bytes = self.read_nv12(2)
        if nv12_bytes is None:
            return None
        return nv12_bytes

    def read_nv12(self, chn: int) -> np.ndarray | None:
        """
        读取NV12数据，并裁剪ROI
        """
        if not self._opened:
            raise RuntimeError("camera is not opened")

        nv12_bytes = self._cam.get_img(chn, self.config.sensor_width, self.config.sensor_height)
        if nv12_bytes is None:
            return None
        nv12_bytes = crop_nv12_roi(nv12_bytes, self.config.sensor_width, self.config.sensor_height, self.config.grab_x, self.config.grab_y, self.config.grab_w, self.config.grab_h)
        nv12_bytes = nv12_adjust_brightness(nv12_bytes, self.config.grab_w, self.config.grab_h, self.config.brightness_delta)
        return np.frombuffer(nv12_bytes, dtype=np.uint8)

    """
    显示流程中使用
    """
    @staticmethod
    def nv12_to_bgr(nv12: np.ndarray, width: int, height: int) -> np.ndarray:
        expected_size = width * height * 3 // 2
        if nv12.size != expected_size:
            raise ValueError(
                f"NV12 buffer size mismatch: got={nv12.size}, expected={expected_size} for {width}x{height}"
            )
        nv12 = nv12.reshape((height * 3 // 2, width))
        return cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)

    @property
    def raw_camera(self):
        return self._cam
