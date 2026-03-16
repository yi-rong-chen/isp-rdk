import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import requests

import src.core.config.global_var as g
import src.core.config.nacos_var as n


class RDKManager:
    """RDK 独立推理进程管理器。"""

    DEFAULT_MODEL_NAME = "best.bin"
    BASE_SENSOR_WIDTH = 1920
    BASE_SENSOR_HEIGHT = 1080
    DEFAULT_SENSOR_SCALE = 1280 / 1920

    def __init__(self):
        self.process_name = g.PROCESS_NAME or Path(os.getcwd()).name or "edge-server"
        self.rdk_dir = Path(os.getcwd()) / "rdk"
        self.start_script = self.rdk_dir / "start.sh"
        self.stop_script = self.rdk_dir / "stop.sh"
        self.config_file = self.rdk_dir / "config.json"
        self.api_port = g.FINISH_API_PORT
        self.is_running = False
        self.process = None

    @staticmethod
    def _normalize_camera_config(camera_config):
        camera_config = camera_config if isinstance(camera_config, dict) else {}
        mipi_config = camera_config.get("mipi")
        if isinstance(mipi_config, dict):
            return "mipi", deepcopy(mipi_config)

        normalized_camera = deepcopy(camera_config)
        normalized_camera.pop("type", None)
        return "mipi", normalized_camera

    @staticmethod
    def _coerce_positive_int(value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _coerce_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_positive_float(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _align_to_even(value):
        aligned = int(float(value) / 2.0 + 0.5) * 2
        return max(2, aligned)

    @classmethod
    def _resolve_sensor_scale(cls, camera_payload):
        sensor_scale = cls._coerce_positive_float(camera_payload.get("sensor_scale"))
        if sensor_scale is not None:
            return sensor_scale

        legacy_sensor_width = cls._coerce_positive_int(camera_payload.get("sensor_width", camera_payload.get("width")))
        if legacy_sensor_width is not None:
            return legacy_sensor_width / cls.BASE_SENSOR_WIDTH

        legacy_sensor_height = cls._coerce_positive_int(camera_payload.get("sensor_height", camera_payload.get("height")))
        if legacy_sensor_height is not None:
            return legacy_sensor_height / cls.BASE_SENSOR_HEIGHT

        return cls.DEFAULT_SENSOR_SCALE

    @classmethod
    def _resolve_display_dimensions(cls, sensor_scale):
        display_width = cls._align_to_even(cls.BASE_SENSOR_WIDTH * sensor_scale)
        display_height = cls._align_to_even(cls.BASE_SENSOR_HEIGHT * sensor_scale)
        return display_width, display_height

    @classmethod
    def _resolve_infer_dimensions(cls, camera_payload):
        img_size = cls._coerce_positive_int(camera_payload.get("img_size"))
        if img_size is not None:
            return img_size, img_size, img_size

        target_resolution = camera_payload.get("target_resolution")
        infer_width = camera_payload.get("infer_width")
        infer_height = camera_payload.get("infer_height")
        if isinstance(target_resolution, dict):
            infer_width = target_resolution.get("width", infer_width)
            infer_height = target_resolution.get("height", infer_height)

        infer_width = cls._coerce_positive_int(infer_width) or 640
        infer_height = cls._coerce_positive_int(infer_height) or 640
        resolved_img_size = infer_width if infer_width == infer_height else None
        return infer_width, infer_height, resolved_img_size

    def _default_model_path(self):
        candidates = [
            self.rdk_dir / "best.bin",
            self.rdk_dir / "yolo11n_bayese_640x640_nv12.bin",
            self.rdk_dir.parent.parent / "rdk-x5-yolo11-web" / self.DEFAULT_MODEL_NAME,
            self.rdk_dir.parent / "yolo11n_bayese_640x640_nv12.bin",
        ]

        for candidate in candidates:
            if candidate.exists():
                return str(candidate.resolve())

        return str(candidates[0])

    def _resolve_backend_config(self, camera_payload):
        infer_width, infer_height, img_size = self._resolve_infer_dimensions(camera_payload)
        sensor_scale = self._resolve_sensor_scale(camera_payload)
        display_width, display_height = self._resolve_display_dimensions(sensor_scale)

        labels_confs = deepcopy(n.LABELS_CONFS) if isinstance(n.LABELS_CONFS, dict) else {}
        label_names = list(labels_confs.keys())
        stream_width = max(1, display_width // 2)
        stream_height = max(1, display_height // 2)

        defaults = {
            "camera_index": int(camera_payload.get("camera_index", 0) or 0),
            "display_chn": int(camera_payload.get("display_chn", camera_payload.get("camera_chn", 2)) or 2),
            "infer_chn": int(camera_payload.get("infer_chn", camera_payload.get("camera_chn", 2)) or 2),
            "sensor_scale": sensor_scale,
            "img_size": img_size,
            "infer_width": infer_width,
            "infer_height": infer_height,
            "classes_num": int(
                camera_payload.get("classes_num") or (len(label_names) if label_names else 80)
            ),
            "reg": int(camera_payload.get("reg", 16) or 16),
            "conf_thres": float(camera_payload.get("conf_thres", 0.25) or 0.25),
            "iou_thres": float(camera_payload.get("iou_thres", 0.45) or 0.45),
            "record_fps": float(camera_payload.get("fps_record", camera_payload.get("fps", 20)) or 20),
            "record_enabled": True,
            "record_original": True,
            "record_labeled": bool(n.DEV_FLAG),
            "record_segment_seconds": int(n.VIDEO_CONFIG.get("segment_seconds", 60) or 60),
            "record_encode_type": 1,
            "record_queue_size": 32,
            "record_original_video_chn": 0,
            "record_labeled_video_chn": 1,
            "stream_width": stream_width,
            "stream_height": stream_height,
            "jpeg_quality": 80,
            "cleanup_retention_hours": 3,
            "cleanup_interval_sec": 300,
            "auto_split_timeout_sec": 3 * 60 * 60,
            "auto_split_check_interval_sec": 10 * 60,
        }

        return defaults

    def generate_config_json(self):
        """生成 RDK 推理进程配置文件。"""
        try:
            if self.config_file.exists():
                self.config_file.unlink()
                g.logger.info(f"已删除现有配置文件: {self.config_file}")

            camera_type, camera_payload = self._normalize_camera_config(n.CAMERA_CONFIG)

            runtime_config = self._resolve_backend_config(camera_payload)
            normalized_camera_payload = deepcopy(camera_payload)
            labels_confs = deepcopy(n.LABELS_CONFS) if isinstance(n.LABELS_CONFS, dict) else {}
            img_size = runtime_config.get("img_size")
            normalized_camera_payload["sensor_scale"] = float(runtime_config.get("sensor_scale", self.DEFAULT_SENSOR_SCALE))
            normalized_camera_payload.pop("sensor_width", None)
            normalized_camera_payload.pop("sensor_height", None)
            if img_size is not None:
                normalized_camera_payload["img_size"] = int(img_size)
                normalized_camera_payload.pop("infer_width", None)
                normalized_camera_payload.pop("infer_height", None)
                normalized_camera_payload.pop("target_resolution", None)

            coerced_brightness_delta = self._coerce_int(normalized_camera_payload.get("brightness_delta"))
            if coerced_brightness_delta is not None:
                normalized_camera_payload["brightness_delta"] = coerced_brightness_delta

            infer_config = {
                "dev_flag": n.DEV_FLAG,
                "api_port": self.api_port,
                "labels_confs": labels_confs,
                "label_names": list(labels_confs.keys()),
                "segment_seconds": n.VIDEO_CONFIG.get("segment_seconds", 60),
                "bitrate_coefficient": n.VIDEO_CONFIG.get("bitrate_coefficient", 1),
            }

            optional_infer_keys = (
                "classes_num",
                "reg",
                "conf_thres",
                "iou_thres",
                "record_fps",
                "record_enabled",
                "record_original",
                "record_labeled",
                "record_segment_seconds",
                "record_encode_type",
                "record_queue_size",
                "record_original_video_chn",
                "record_labeled_video_chn",
                "stream_width",
                "stream_height",
                "jpeg_quality",
                "cleanup_retention_hours",
                "cleanup_interval_sec",
                "auto_split_timeout_sec",
                "auto_split_check_interval_sec",
            )
            for key in optional_infer_keys:
                if key in runtime_config:
                    infer_config[key] = runtime_config[key]

            default_model_path = str(Path(self._default_model_path()).resolve())
            custom_model_path = runtime_config.get("model_path")
            if custom_model_path:
                custom_model_path = str(custom_model_path)
                resolved_custom_model = str((self.rdk_dir / custom_model_path).resolve()) if not Path(custom_model_path).is_absolute() else str(Path(custom_model_path).resolve())
                if resolved_custom_model != default_model_path:
                    infer_config["model_path"] = custom_model_path

            config = {
                "camera": normalized_camera_payload,
                "infer": infer_config,
            }

            self.rdk_dir.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as file:
                json.dump(config, file, ensure_ascii=False, indent=4)

            g.logger.info(f"已生成 RDK 配置文件: {self.config_file}")
            g.logger.info(f"RDK 配置已更新，相机类型: {camera_type}")
            return True
        except Exception as exc:
            g.logger.error(f"生成 RDK 配置文件失败: {exc}")
            raise

    def split_stream(self):
        """截断当前视频片段，返回(原始视频相对路径, 标注视频相对路径)。"""
        try:
            g.logger.info(f"开始调用 RDK split 接口，端口: {self.api_port}")
            response = requests.post(f"http://localhost:{self.api_port}/split", timeout=10)
            g.logger.info(f"split 接口响应状态: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                files = data.get("files", {}) if isinstance(data, dict) else {}
                original_filename = files.get("original")
                labeled_filename = files.get("labeled")
                original_file = f"upload/original/{original_filename}" if original_filename else None
                labeled_file = f"upload/label/{labeled_filename}" if labeled_filename else None
                g.logger.info(f"视频分段完成: original={original_file}, labeled={labeled_file}")
                return original_file, labeled_file

            g.logger.error(f"分段结束失败: {response.status_code} - {response.text}")
            return None, None
        except requests.exceptions.Timeout as exc:
            g.logger.error(f"调用分段接口超时: {exc}")
            return None, None
        except requests.exceptions.ConnectionError as exc:
            g.logger.error(f"连接分段接口失败: {exc}")
            return None, None
        except Exception as exc:
            g.logger.error(f"调用分段接口失败: {exc}")
            return None, None

    def start_rdk(self):
        """启动 RDK 推理进程。"""
        try:
            self.generate_config_json()

            if not self.start_script.exists():
                g.logger.error(f"启动脚本不存在: {self.start_script}")
                return False

            cmd = ["bash", str(self.start_script), self.process_name]
            g.logger.info("正在启动 RDK 推理进程")
            g.logger.info(f"执行命令: {' '.join(cmd)}")
            g.logger.info(f"工作目录: {self.rdk_dir}")

            log_file = self.rdk_dir / "rdk.log"
            with open(log_file, "a", encoding="utf-8") as file:
                self.process = subprocess.Popen(
                    cmd,
                    env=os.environ.copy(),
                    stdout=file,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.rdk_dir),
                )

            self.is_running = True
            g.logger.info(f"RDK 推理进程已启动，PID: {self.process.pid}")
            g.logger.info(f"日志文件: {log_file}")
            return True
        except Exception as exc:
            g.logger.error(f"启动 RDK 推理进程失败: {exc}")
            self.is_running = False
            return False

    def stop_rdk(self):
        """停止 RDK 推理进程。"""
        try:
            if not self.stop_script.exists():
                g.logger.error(f"停止脚本不存在: {self.stop_script}")
                return False

            cmd = ["bash", str(self.stop_script), self.process_name]
            g.logger.info("正在停止 RDK 推理进程")
            g.logger.info(f"执行命令: {' '.join(cmd)}")
            g.logger.info(f"工作目录: {self.rdk_dir}")

            result = subprocess.run(
                cmd,
                cwd=str(self.rdk_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                g.logger.info("RDK 推理进程已停止")
                if result.stdout:
                    g.logger.info(f"停止脚本输出: {result.stdout}")
            else:
                g.logger.warning(f"停止脚本执行失败，返回码: {result.returncode}")
                if result.stderr:
                    g.logger.warning(f"错误输出: {result.stderr}")

            self.is_running = False
            self.process = None
            return True
        except subprocess.TimeoutExpired:
            g.logger.error("停止脚本执行超时")
            return False
        except Exception as exc:
            g.logger.error(f"停止 RDK 推理进程失败: {exc}")
            return False

    def set_disappear_line(self, disappear_line):
        """设置检测线。"""
        try:
            g.logger.info(f"开始设置检测线: {disappear_line}")
            response = requests.post(
                f"http://localhost:{self.api_port}/disappear_line",
                json={"x": disappear_line},
                timeout=5,
            )
            g.logger.info(f"检测线接口响应状态: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                g.logger.info(f"检测线设置成功: x={data.get('x')}")
                return True

            g.logger.error(f"检测线设置失败: {response.status_code} - {response.text}")
            return False
        except requests.exceptions.Timeout as exc:
            g.logger.error(f"设置检测线接口超时: {exc}")
            return False
        except requests.exceptions.ConnectionError as exc:
            g.logger.error(f"连接检测线接口失败: {exc}")
            return False
        except Exception as exc:
            g.logger.error(f"设置检测线失败: {exc}")
            return False
