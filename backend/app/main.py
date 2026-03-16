from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import socketio

from app.core.logging import setup_logging
from app.routers.stream import router as stream_router
from app.services.pipeline import InferencePipeline

setup_logging()
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pipeline = InferencePipeline()
    pipeline.start()
    loop = asyncio.get_running_loop()

    def on_detection(data: dict) -> None:
        loop.call_soon_threadsafe(asyncio.create_task, sio.emit("detections", data))

    pipeline.set_detection_callback(on_detection)
    app.state.pipeline = pipeline
    try:
        yield
    finally:
        pipeline.set_detection_callback(None)
        pipeline.stop()


fastapi_app = FastAPI(title="RDK YOLO Stream Backend", version="0.1.0", lifespan=lifespan)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
fastapi_app.include_router(stream_router)

# Wrap FastAPI with Socket.IO ASGI app to avoid websocket path mismatch errors.
app = socketio.ASGIApp(
    sio,
    other_asgi_app=fastapi_app,
    socketio_path="ws/socket.io",
)
