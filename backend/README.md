# Backend

将原始相机 + 推理 demo 工程化为 FastAPI 服务，提供推理后视频流接口。

## 目录

- `app/services/yolo_infer.py`: 模型加载、预处理、推理、后处理、绘制
- `app/services/camera.py`: MIPI 相机封装
- `app/services/pipeline.py`: 推理流水线封装（相机 + 模型）
- `app/routers/stream.py`: 接口定义（`/health`, `/stream`）
- `app/main.py`: FastAPI 应用生命周期管理

## 运行

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 run.py
```

## 接口

- 健康检查: `GET /health`
- 推理视频流（MJPEG）: `GET /stream`
- 实时检测数据: `GET /detections`
- 实时检测推送（Socket.IO）: `ws://<host>:8000/ws/socket.io`，事件名 `detections`

示例：

```bash
curl "http://127.0.0.1:8000/stream?fps=20&quality=80"
```

浏览器访问：

- [http://127.0.0.1:8000/stream](http://127.0.0.1:8000/stream)

## 环境变量

- `MODEL_PATH`
- `CLASSES_NUM`
- `REG`
- `CONF_THRES`
- `IOU_THRES`
- `CAMERA_INDEX`
- `CAMERA_CHN`
- `INFER_CHN`（推理通道，默认与 `CAMERA_CHN` 一致）
- `SENSOR_WIDTH`
- `SENSOR_HEIGHT`
- `DISPLAY_WIDTH`
- `DISPLAY_HEIGHT`
- `JPEG_QUALITY`
- `STREAM_FPS`
- `PROCESS_FPS`（默认 `0`，表示不限制处理帧率；例如 `10` 表示推理/叠框按约 10fps 处理）
- `CAMERA_READ_FPS`（默认 `0`，表示不限制取帧速率；例如 `10` 表示软件侧按约 10fps 采样相机帧）
- `RECORD_ENABLED`（默认 `1`，开启硬件编码录像）
- `RECORD_DIR`（默认当前工程 `video/`）
- `RECORD_SEGMENT_SECONDS`（默认 `60`，按分钟切片）
- `RECORD_ENCODE_TYPE`（`1=H264`, `2=H265`, `3=MJPEG`）
- `RECORD_BITRATE_KBPS`（默认 `8000`）
- `RECORD_FPS`（默认 `0`，表示不降帧；例如设置 `10` 表示只录制约 10fps）

说明：
- 推理输入分辨率固定使用模型输入尺寸（自动从模型读取，例如 `640x640`）。
- 当前接口返回大路显示流（`CAMERA_CHN`），推理走小路（`INFER_CHN`），并将小路检测框映射后叠加到大路图像。
- 录像使用非 `bind` 模式：相机 NV12 帧进入队列，编码线程通过 `encode_file/get_img` 写文件，默认每 60 秒生成一个新文件到 `video/`。
- 当 `RECORD_ENCODE_TYPE` 为 `1/2`（H264/H265）时，显示通道分辨率会自动按 8 对齐（例如 `960x540` 会调整为 `960x544`）。
- 录像文件按天存储在 `video/YYYY-MM-DD/`；当磁盘剩余空间低于 1GB 时，会自动删除最旧录像文件直到恢复到安全阈值。
