# -*- coding: utf-8 -*-
"""
进程间传输 labels 的轻量 IPC 封装（ZeroMQ + UNIX 域套接字）。

改进点：
- SUB 端：先 SUBSCRIBE 再 connect，避免慢启动期间丢订阅。
- PUB 端：bind 后延迟发送；可选“等订阅者就绪再开始发”（require_subscriber=True）。
- IPC：bind 成功后 chmod 以避免跨用户/容器的权限问题；启动前清理残留文件。
- 仍默认非阻塞与丢帧策略确保实时性；日志可开关。
"""

import os
import json
import time
import threading
import queue
from typing import List, Optional

try:
    import zmq  # type: ignore
except Exception as e:  # pragma: no cover
    zmq = None


class LabelsPublisher:
    """后台线程发布 labels，线程安全地从任意线程 publish。"""

    def __init__(
        self,
        endpoint: str,
        queue_maxsize: int = 32,
        sndhwm: int = 8,
        conflate: bool = False,  # 修复：默认禁用conflate，避免消息丢失
        warmup_ms: int = 300,
        require_subscriber: bool = False,
        verbose: bool = False,
    ):
        if zmq is None:
            raise RuntimeError("pyzmq 未安装，无法创建 LabelsPublisher")

        self._endpoint = endpoint
        self._q: "queue.Queue[List[List[int]]]" = queue.Queue(maxsize=queue_maxsize)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ctx = zmq.Context()
        self._verbose = verbose
        self._warmup_ms = warmup_ms
        self._require_sub = require_subscriber

        # 清理残留的 ipc 文件
        if endpoint.startswith("ipc://"):
            path = endpoint.replace("ipc://", "")
            try:
                if os.path.exists(path):
                    os.remove(path)
                    if self._verbose:
                        print(f"[PUB] removed stale ipc file: {path}")
            except Exception as e:
                if self._verbose:
                    print(f"[PUB] remove stale ipc file failed: {e}")

        # 如果需要探测订阅者，用 XPUB；否则普通 PUB
        sock_type = zmq.XPUB if self._require_sub else zmq.PUB
        self._sock = self._ctx.socket(sock_type)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.SNDHWM, sndhwm)
        if conflate:
            try:
                self._sock.setsockopt(zmq.CONFLATE, 1)
            except Exception:
                pass

        # 使 XPUB 能接收订阅事件
        if sock_type == zmq.XPUB:
            try:
                self._sock.setsockopt(zmq.XPUB_VERBOSE, 1)
            except Exception:
                pass

        self._sock.bind(endpoint)

        # 尝试放宽 ipc 节点权限（不一定在所有平台生效，但大多数 Linux 可行）
        if endpoint.startswith("ipc://"):
            path = endpoint.replace("ipc://", "")
            try:
                os.chmod(path, 0o666)
                if self._verbose:
                    print(f"[PUB] chmod 666 {path}")
            except Exception as e:
                if self._verbose:
                    print(f"[PUB] chmod failed: {e}")

        self._thread.start()

    def publish(self, labels: List[List[int]]):
        """入队一帧 labels；队列满时丢弃该帧。"""
        if labels is None:
            return
        try:
            self._q.put_nowait(labels)
        except queue.Full:
            # 丢帧以保证实时性
            pass

    def close(self):
        try:
            self._stop.set()
            try:
                self._q.put_nowait([])  # 唤醒
            except queue.Full:
                pass
            self._thread.join(timeout=2)
        finally:
            try:
                self._sock.close(0)
            except Exception:
                pass
            try:
                self._ctx.term()
            except Exception:
                pass

    def _wait_for_first_subscriber(self, timeout_s: float) -> bool:
        """仅在 XPUB 下生效：等待首个订阅事件，返回是否检测到。"""
        if self._sock.type != zmq.XPUB:
            return True
        poller = zmq.Poller()
        poller.register(self._sock, zmq.POLLIN)
        t0 = time.time()
        while (time.time() - t0) < timeout_s and not self._stop.is_set():
            socks = dict(poller.poll(timeout=100))
            if self._sock in socks and socks[self._sock] & zmq.POLLIN:
                try:
                    msg = self._sock.recv(flags=zmq.NOBLOCK)
                    # XPUB 订阅事件：首字节 0x01 = 订阅，0x00 = 取消订阅
                    if msg and msg[0] == 1:
                        if self._verbose:
                            print("[PUB] subscriber detected")
                        return True
                except zmq.Again:
                    pass
        return False

    # 后台发送线程（唯一持有 socket 的线程）
    def _run(self):
        # bind 后给订阅建立留点时间
        if self._warmup_ms > 0:
            time.sleep(self._warmup_ms / 1000.0)

        # 如需：等到有订阅者再开始发
        if self._require_sub:
            ok = self._wait_for_first_subscriber(timeout_s=5.0)
            if not ok and self._verbose:
                print("[PUB] no subscriber detected within timeout; still sending...")

        while not self._stop.is_set():
            try:
                labels = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            if self._stop.is_set():
                break
            try:
                payload = json.dumps(labels, separators=(",", ":")).encode("utf-8")
                # multipart: [topic, payload]
                self._sock.send_multipart([b"labels", payload], flags=zmq.NOBLOCK)
            except Exception as e:
                # 忽略发送异常（例如无订阅者、HWM 满等）
                if self._verbose:
                    print(f"[PUB] send failed: {e}")



