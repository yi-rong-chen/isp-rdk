from dataclasses import dataclass
from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    model_path: str = os.getenv("MODEL_PATH", str(PROJECT_ROOT / "yolo11n_bayese_640x640_nv12.bin"))
    classes_num: int = int(os.getenv("CLASSES_NUM", "80"))
    reg: int = int(os.getenv("REG", "16"))
    conf_thres: float = float(os.getenv("CONF_THRES", "0.25"))
    iou_thres: float = float(os.getenv("IOU_THRES", "0.25"))

    camera_index: int = int(os.getenv("CAMERA_INDEX", "0"))
    camera_chn: int = int(os.getenv("CAMERA_CHN", "0"))
    infer_chn: int = int(os.getenv("INFER_CHN", "2"))
    sensor_width: int = int(os.getenv("SENSOR_WIDTH", "1280"))
    sensor_height: int = int(os.getenv("SENSOR_HEIGHT", "720"))
    grab_x: int = int(os.getenv("GRAB_X", "200"))
    grab_y: int = int(os.getenv("GRAB_Y", "200"))
    grab_w: int = int(os.getenv("GRAB_W", "960"))
    grab_h: int = int(os.getenv("GRAB_H", "480"))
    brightness_delta: int = int(os.getenv("BRIGHTNESS_DELTA", "0"))
    infer_width: int = 640
    infer_height: int = 640

    jpeg_quality: int = int(os.getenv("JPEG_QUALITY", "80"))
    stream_fps: int = int(os.getenv("STREAM_FPS", "20"))
    process_fps: float = float(os.getenv("PROCESS_FPS", "20"))
    camera_read_fps: float = float(os.getenv("CAMERA_READ_FPS", "20"))
    
    record_enabled: bool = os.getenv("RECORD_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    record_dir: str = os.getenv("RECORD_DIR", str(PROJECT_ROOT / "video"))
    record_segment_seconds: int = int(os.getenv("RECORD_SEGMENT_SECONDS", "60"))
    record_encode_type: int = int(os.getenv("RECORD_ENCODE_TYPE", "2"))
    record_bitrate_kbps: int = int(os.getenv("RECORD_BITRATE_KBPS", "8000"))
    record_fps: float = float(os.getenv("RECORD_FPS", "20"))


settings = Settings()
