import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Empty } from "antd";
import "./index.scss";

interface LiveVideoProps {
  appUrl: string;
  streaming: boolean;
  streamUrl?: string;
  onStreamError?: () => void;
  onStopFeeding?: () => Promise<void>;
  onStartFeeding?: () => Promise<void>;
}

const HEALTH_CHECK_INTERVAL_MS = 4000;
const FRAME_TIMEOUT_THRESHOLD_MS = 12000;
const RECONNECT_DELAY_MS = 2000;
const STREAM_FPS = 20;
const STREAM_QUALITY = 70;

const appendCacheBuster = (url: string, token: number) => {
  try {
    const parsed = new URL(url, window.location.origin);
    parsed.searchParams.set("_ts", String(token));
    return parsed.toString();
  } catch {
    return `${url}${url.includes("?") ? "&" : "?"}_ts=${token}`;
  }
};

const normalizeStreamUrl = (url: string) => {
  if (!url) {
    return "";
  }

  try {
    const parsed = new URL(url, window.location.origin);
    if (!parsed.searchParams.has("fps")) {
      parsed.searchParams.set("fps", String(STREAM_FPS));
    }
    if (!parsed.searchParams.has("quality")) {
      parsed.searchParams.set("quality", String(STREAM_QUALITY));
    }
    return parsed.toString();
  } catch {
    const separator = url.includes("?") ? "&" : "?";
    return `${url}${separator}fps=${STREAM_FPS}&quality=${STREAM_QUALITY}`;
  }
};

const LiveVideo: React.FC<LiveVideoProps> = ({
  appUrl,
  streaming,
  streamUrl,
  onStreamError,
}: LiveVideoProps) => {
  const [fps, setFps] = useState<number>(0);
  const [isStreamHealthy, setIsStreamHealthy] = useState<boolean>(true);
  const [renderUrl, setRenderUrl] = useState<string>("");
  const [statusText, setStatusText] = useState<string>("等待连接");
  const imageRef = useRef<HTMLImageElement | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const healthCheckTimerRef = useRef<number | null>(null);
  const reconnectingRef = useRef<boolean>(false);
  const lastFrameTimeRef = useRef<number>(Date.now());
  const firstFrameSeenRef = useRef<boolean>(false);

  const normalizedStreamUrl = useMemo(() => normalizeStreamUrl(streamUrl?.trim() || ""), [streamUrl]);

  const clearTimers = () => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (healthCheckTimerRef.current) {
      window.clearInterval(healthCheckTimerRef.current);
      healthCheckTimerRef.current = null;
    }
  };

  const scheduleReconnect = useCallback(() => {
    if (!streaming || !normalizedStreamUrl || reconnectingRef.current) {
      return;
    }

    reconnectingRef.current = true;
    setIsStreamHealthy(false);
    setStatusText("连接异常，正在重连...");
    firstFrameSeenRef.current = false;

    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
    }

    reconnectTimerRef.current = window.setTimeout(() => {
      reconnectTimerRef.current = null;
      reconnectingRef.current = false;
      setRenderUrl(appendCacheBuster(normalizedStreamUrl, Date.now()));
      lastFrameTimeRef.current = Date.now();
      setStatusText("重新拉取视频流");
    }, RECONNECT_DELAY_MS);
  }, [normalizedStreamUrl, streaming]);

  useEffect(() => {
    if (!streaming || !normalizedStreamUrl) {
      clearTimers();
      reconnectingRef.current = false;
      firstFrameSeenRef.current = false;
      setRenderUrl("");
      setFps(0);
      setIsStreamHealthy(true);
      setStatusText("视频流未开启");
      return;
    }

    reconnectingRef.current = false;
    firstFrameSeenRef.current = false;
    lastFrameTimeRef.current = Date.now();
    setRenderUrl(appendCacheBuster(normalizedStreamUrl, Date.now()));
    setStatusText("开始拉取 MJPEG 视频流");

    if (appUrl) {
      healthCheckTimerRef.current = window.setInterval(async () => {
        try {
          const response = await fetch(`${appUrl}/status`, { method: "GET" });
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
          }

          const payload = await response.json();
          if (typeof payload?.fps === "number") {
            setFps(payload.fps);
          }

          const lastFrameTs = Number(payload?.last_frame_ts || 0) * 1000;
          if (lastFrameTs > 0) {
            lastFrameTimeRef.current = lastFrameTs;
          }

          if (Date.now() - lastFrameTimeRef.current > FRAME_TIMEOUT_THRESHOLD_MS) {
            scheduleReconnect();
          }
        } catch (error) {
          console.warn("[LiveVideo] 状态检查失败:", error);
        }
      }, HEALTH_CHECK_INTERVAL_MS);
    }

    return () => {
      clearTimers();
      reconnectingRef.current = false;
    };
  }, [appUrl, normalizedStreamUrl, streaming]);

  useEffect(() => {
    const image = imageRef.current;
    if (!image || !streaming || !renderUrl) {
      return;
    }

    image.onload = () => {
      if (firstFrameSeenRef.current) {
        return;
      }

      firstFrameSeenRef.current = true;
      reconnectingRef.current = false;
      lastFrameTimeRef.current = Date.now();
      setIsStreamHealthy(true);
      setStatusText("MJPEG 已连接");
    };

    image.onerror = () => {
      onStreamError?.();
      scheduleReconnect();
    };

    return () => {
      image.onload = null;
      image.onerror = null;
    };
  }, [onStreamError, renderUrl, scheduleReconnect, streaming]);

  return (
    <div className="live-video-container">
      {streaming && normalizedStreamUrl ? (
        <>
          <img
            ref={imageRef}
            src={renderUrl}
            className="live-video-stream"
            style={{ width: "100%", height: "100%", opacity: isStreamHealthy ? 1 : 0.5 }}
            alt="live stream"
          />
          <div className="fps-display">
            {fps.toFixed(1)} FPS
            {!isStreamHealthy && (
              <span style={{ color: "#ff4d4f", marginLeft: 10 }}>{statusText}</span>
            )}
          </div>
        </>
      ) : (
        <Empty className="live-video-empty" description="视频流未开启" />
      )}
    </div>
  );
};

export default memo(
  LiveVideo,
  (prevProps, nextProps) =>
    prevProps.appUrl === nextProps.appUrl &&
    prevProps.streaming === nextProps.streaming &&
    prevProps.streamUrl === nextProps.streamUrl &&
    prevProps.onStreamError === nextProps.onStreamError
);
