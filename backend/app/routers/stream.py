from __future__ import annotations

from time import monotonic, sleep

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import settings

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/detections")
def detections(request: Request) -> JSONResponse:
    pipeline = request.app.state.pipeline
    return JSONResponse(pipeline.get_latest_detections())


@router.get("/stream")
def stream_video(
    request: Request,
    fps: int = Query(default=settings.stream_fps, ge=1, le=60),
    quality: int = Query(default=settings.jpeg_quality, ge=30, le=95),
) -> StreamingResponse:
    pipeline = request.app.state.pipeline

    def gen():
        interval = 1.0 / fps
        last_seq = -1
        while True:
            t0 = monotonic()
            try:
                jpg, last_seq = pipeline.next_jpeg(quality, last_seq=last_seq, timeout=interval * 1.5)
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                )
            except Exception:
                sleep(0.05)
            spent = monotonic() - t0
            remain = interval - spent
            if remain > 0:
                sleep(remain)

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame", headers=headers)
