from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import logging
import queue
import shutil
import threading
import time

import numpy as np

try:
    from hobot_vio import libsrcampy
except Exception:  # pragma: no cover
    libsrcampy = None

logger = logging.getLogger("RDK_RECORDER")


@dataclass(frozen=True)
class RecorderConfig:
    output_dir: str
    width: int
    height: int
    segment_seconds: int = 60
    encode_type: int = 1  # 1: H264, 2: H265, 3: MJPEG
    video_chn: int = 0
    bitrate_kbps: int = 8000
    queue_size: int = 120


class VideoRecorder:
    MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024  # 1GB

    def __init__(self, config: RecorderConfig) -> None:
        if libsrcampy is None:
            raise RuntimeError("hobot_vio.libsrcampy is unavailable. Run this service on an RDK device.")

        self.config = config
        self._encoder = None
        self._outfile = None
        self._current_path: Path | None = None
        self._segment_start = 0.0

        self._q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=max(1, config.queue_size))
        self._worker: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return

        encoder_cls = getattr(libsrcampy, "Encoder", None) or getattr(libsrcampy, "Encode", None)
        if encoder_cls is None:
            raise RuntimeError("No Encoder/Encode class found in hobot_vio.libsrcampy")

        self._encoder = encoder_cls()
        cfg = self.config

        enc_w = cfg.width
        enc_h = cfg.height
        if cfg.encode_type in (1, 2):
            enc_w = ((enc_w + 7) // 8) * 8
            enc_h = ((enc_h + 7) // 8) * 8

        try:
            ret = self._encoder.encode(cfg.video_chn, cfg.encode_type, enc_w, enc_h)
        except TypeError:
            ret = self._encoder.encode(cfg.video_chn, cfg.encode_type, enc_w, enc_h)
        if ret != 0:
            raise RuntimeError(f"failed to start encoder: ret={ret}")

        Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
        self._rotate_file(initial=True)

        self._running = True
        self._worker = threading.Thread(target=self._run, name="video-recorder", daemon=True)
        self._worker.start()
        logger.info(
            "recorder started (queue mode): dir=%s chn=%d resolution=%dx%d segment=%ss",
            cfg.output_dir,
            cfg.video_chn,
            enc_w,
            enc_h,
            cfg.segment_seconds,
        )

    def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

        if self._worker is not None:
            self._worker.join(timeout=3.0)
            self._worker = None

        # Try to fetch a final packet once before closing.
        self._fetch_one_packet()

        if self._outfile is not None:
            self._outfile.flush()
            self._outfile.close()
            self._outfile = None
            self._current_path = None

        if self._encoder is not None:
            ret = self._encoder.close()
            if ret != 0:
                logger.warning("encoder close returned non-zero: %s", ret)
            self._encoder = None

        logger.info("recorder stopped")

    def enqueue_nv12(self, nv12: np.ndarray) -> None:
        if not self._running:
            return

        expected_size = self.config.width * self.config.height * 3 // 2
        if nv12.size != expected_size:
            logger.warning(
                "skip record frame due to size mismatch: got=%d expected=%d",
                nv12.size,
                expected_size,
            )
            return

        frame = nv12.copy()

        try:
            self._q.put_nowait(frame)
        except queue.Full:
            # Keep latest frame by dropping one old item.
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass

    def _run(self) -> None:
        while self._running or not self._q.empty():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            if item is None:
                break

            if time.time() - self._segment_start >= self.config.segment_seconds:
                self._rotate_file(initial=False)

            ret = self._encoder.encode_file(item.tobytes())
            if ret != 0:
                logger.warning("encode_file failed: ret=%s", ret)
                continue

            # Do not loop-drain here: repeated empty get_img() calls can trigger
            # dequeue timeout errors in the underlying codec.
            self._fetch_one_packet()

    def _fetch_one_packet(self) -> None:
        if self._encoder is None or self._outfile is None:
            return

        packet = self._encoder.get_img()
        if packet is None:
            return
        self._outfile.write(packet)

    def _rotate_file(self, initial: bool) -> None:
        if self._outfile is not None:
            self._outfile.flush()
            self._outfile.close()
            self._outfile = None
            self._current_path = None

        self._cleanup_old_videos_if_needed()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        day_dir = Path(self.config.output_dir) / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        suffix = "h264" if self.config.encode_type == 1 else ("h265" if self.config.encode_type == 2 else "mjpg")
        filename = f"display_{ts}.{suffix}"
        path = day_dir / filename
        self._outfile = open(path, "wb")
        self._current_path = path
        self._segment_start = time.time()

        if initial:
            logger.info("recording file opened: %s", path)
        else:
            logger.info("recording file rotated: %s", path)

    def _cleanup_old_videos_if_needed(self) -> None:
        root = Path(self.config.output_dir)
        root.mkdir(parents=True, exist_ok=True)

        def free_bytes() -> int:
            usage = shutil.disk_usage(root)
            return usage.free

        if free_bytes() >= self.MIN_FREE_BYTES:
            return

        suffixes = {".h264", ".h265", ".mjpg"}
        files = sorted(
            [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes],
            key=lambda p: p.stat().st_mtime,
        )

        removed = 0
        for p in files:
            if self._current_path is not None and p.resolve() == self._current_path.resolve():
                continue
            try:
                p.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except Exception:
                logger.exception("failed to delete old video file: %s", p)

            if free_bytes() >= self.MIN_FREE_BYTES:
                break

        logger.warning(
            "disk cleanup finished: removed=%d, free=%.2fGB, threshold=1.00GB",
            removed,
            free_bytes() / (1024 ** 3),
        )
