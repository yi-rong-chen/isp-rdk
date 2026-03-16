#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
from typing import Dict, Any, Tuple, List
from logger_config import get_logger

# 创建日志器 - 使用 RDK 日志名称
logger = get_logger(__name__, log_name='rdk')

class ConfigManager:
    DEFAULT_RTSP_PORT = 554
    DEFAULT_RTSP_PATH_TEMPLATES = [
        "/Streaming/Channels/101",                   # 海康主码流
        "/cam/realmonitor?channel=1&subtype=0",     # 大华主码流
        "/h264/ch1/main/av_stream",                  # 常见兼容路径（主码流）
    ]

    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
            
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_camera_type(self) -> str:
        """获取相机类型"""
        return self.config.get("camera", {}).get("type", "").lower()

    def get_gige_config(self) -> Dict[str, Any]:
        """获取GIGE相机配置"""
        return self.config.get("camera", {}).get("gige", {})

    @staticmethod
    def _normalize_rtsp_path(path: str) -> str:
        if not path:
            return ""
        path = str(path).strip()
        if path.startswith("rtsp://"):
            return path
        return path if path.startswith("/") else f"/{path}"

    @staticmethod
    def _build_rtsp_uri(ip: str, user: str, password: str, path_or_uri: str) -> str:
        normalized = ConfigManager._normalize_rtsp_path(path_or_uri)
        if normalized.startswith("rtsp://"):
            return normalized
        if user and password:
            return f"rtsp://{user}:{password}@{ip}:{ConfigManager.DEFAULT_RTSP_PORT}{normalized}"
        return f"rtsp://{ip}:{ConfigManager.DEFAULT_RTSP_PORT}{normalized}"

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def get_rtsp_candidate_sources(self) -> List[str]:
        """返回 RTSP 候选地址列表（模板轮询用）"""
        rtsp_config = self.config.get("camera", {}).get("rtsp", {})
        ip = str(rtsp_config.get("ip", "")).strip()
        user = str(rtsp_config.get("user", "")).strip()
        password = str(rtsp_config.get("pass", "")).strip()

        configured_path = rtsp_config.get("path", "")
        configured_templates = rtsp_config.get("path_templates", [])
        if not isinstance(configured_templates, list):
            configured_templates = []

        raw_paths = []
        if configured_path:
            raw_paths.append(configured_path)
        raw_paths.extend(configured_templates)
        raw_paths.extend(self.DEFAULT_RTSP_PATH_TEMPLATES)

        candidates = []
        seen = set()

        for raw in raw_paths:
            path_or_uri = self._normalize_rtsp_path(str(raw))
            if not path_or_uri:
                continue
            uri = self._build_rtsp_uri(ip, user, password, path_or_uri)
            if uri not in seen:
                candidates.append(uri)
                seen.add(uri)

        # 兼容旧配置：若未配置 path/path_templates，至少返回一个基础地址
        if not candidates:
            candidates.append(self._build_rtsp_uri(ip, user, password, ""))

        return candidates
    
    def get_video_source(self) -> str:
        """根据相机类型返回视频源"""
        camera_type = self.get_camera_type()
        camera_config = self.config.get("camera", {})
        
        if camera_type == "rtsp":
            candidates = self.get_rtsp_candidate_sources()
            return candidates[0] if candidates else ""
            
        elif camera_type == "gige":
            gige_config = camera_config.get("gige", {})
            return gige_config.get('camera_name', '')
            
        else:
            raise ValueError(f"不支持的相机类型: {camera_type}")
    
    def get_video_params(self) -> Tuple[int, int, int, int]:
        """获取视频参数(width, height, fps, fps_record)"""
        camera_type = self.get_camera_type()
        camera_config = self.config.get("camera", {})
        
        if camera_type == "gige":
            gige_config = camera_config.get("gige", {})
            return (
                gige_config.get("width", 1280),
                gige_config.get("height", 720),
                gige_config.get("fps", 30),
                gige_config.get("fps_record", 5)
            )
        elif camera_type == "rtsp":
            rtsp_config = camera_config.get("rtsp", {})
            return (
                1280,  # RTSP 分辨率运行时自动探测，此处仅作启动回退值
                720,
                rtsp_config.get("fps", 0),  # 0 表示运行时自动探测相机 FPS
                rtsp_config.get("fps_record", 5)
            )
        else:  # 其他类型使用默认参数
            return (1280, 720, 30, 5)
    
    def get_segment_time(self) -> int:
        """获取视频分段时间"""
        return self.config.get("infer", {}).get("segment_seconds", 10)
    
    def get_bitrate_coefficient(self) -> float:
        """获取码率系数"""
        return self.config.get("infer", {}).get("bitrate_coefficient", 1)
    
    def get_labels_confs(self) -> Dict[str, float]:
        """获取标签阈值配置"""
        return self.config.get("infer", {}).get("labels_confs", {})
    
    def get_dev_flag(self) -> bool:
        """获取开发标志，True为检测模式，False为录制模式"""
        return self.config.get("infer", {}).get("dev_flag", True)
    
    def get_api_flag(self) -> bool:
        """获取API标志，True为API模式，False为定时器模式（仅在dev_flag为True时生效）"""
        return self.config.get("infer", {}).get("api_flag", False)
    
    def get_api_port(self) -> int:
        """获取API服务器端口"""
        return self.config.get("infer", {}).get("api_port")
    
    def get_webrtc_signaling_url(self) -> str:
        """获取WebRTC信令服务器URL"""
        return self.config.get("infer", {}).get("webrtc_signaling_url", "ws://127.0.0.1:8765")

    def get_img_size(self) -> Tuple[int, bool]:
        """获取方形模型输入尺寸配置 (img_size, enabled)"""
        camera_type = self.get_camera_type()
        camera_config = self.config.get("camera", {}).get(camera_type, {})

        img_size = self._coerce_positive_int(camera_config.get("img_size"))
        if img_size is not None:
            return (img_size, True)

        target_config = camera_config.get("target_resolution")
        if isinstance(target_config, dict):
            width = self._coerce_positive_int(target_config.get("width"))
            height = self._coerce_positive_int(target_config.get("height"))
            if width is not None and height is not None and width == height:
                return (width, True)

        infer_width = self._coerce_positive_int(camera_config.get("infer_width"))
        infer_height = self._coerce_positive_int(camera_config.get("infer_height"))
        if infer_width is not None and infer_height is not None and infer_width == infer_height:
            return (infer_width, True)

        return (640, False)
    
    def get_target_resolution(self) -> Tuple[int, int, bool]:
        """
        获取目标分辨率配置 (width, height, enabled)
        优化点：
        - 减少不必要的中间变量
        - 更健壮地判断配置是否合法
        - 增加类型及内容校验
        """
        camera_type = self.get_camera_type()
        camera_config = self.config.get("camera", {}).get(camera_type, {})

        img_size, enabled = self.get_img_size()
        if enabled:
            return (img_size, img_size, True)

        target_config = camera_config.get("target_resolution")
        if isinstance(target_config, dict):
            width = self._coerce_positive_int(target_config.get("width"))
            height = self._coerce_positive_int(target_config.get("height"))
            if width is not None and height is not None:
                return (width, height, True)

        infer_width = self._coerce_positive_int(camera_config.get("infer_width"))
        infer_height = self._coerce_positive_int(camera_config.get("infer_height"))
        if infer_width is not None and infer_height is not None:
            return (infer_width, infer_height, True)

        return (640, 480, False)
