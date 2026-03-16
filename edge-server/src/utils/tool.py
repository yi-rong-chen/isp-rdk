from datetime import datetime
import os
import json
import threading
import time
from collections import deque
import requests
import src.core.config.global_var as g
import cv2
import src.core.config.nacos_var as n
from src.utils.run_str_code import PythonRunner
import traceback
from typing import List, Optional

# 尝试导入 zmq，如果失败则设置为 None
try:
    import zmq
except ImportError:
    zmq = None

exec_python_code_runner_dict = {}

def init_counting(current_status, task_id):
    count_path = "./count/count_{}_{}.json"

    # 确保 count 目录存在
    count_dir = "./count/"
    if not os.path.exists(count_dir):
        os.makedirs(count_dir)  # 创建 count 目录

    # 获取当前日期
    now = datetime.now()
    formatted_date = now.strftime('%Y-%m-%d')

    # 当前任务的计数文件
    filename = count_path.format(formatted_date, task_id)

    # 初始化当前任务的计数数据
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as file:
                data = json.load(file)
                current_status["counting"] = data.get("counting", 0)  # 当前任务计数
                current_status["failed_counting"] = data.get("failed_counting", 0)  # 当前任务失败计数
        except (json.JSONDecodeError, FileNotFoundError) as e:
            # 如果发生异常，初始化为 0
            current_status["counting"] = 0
            current_status["failed_counting"] = 0
            g.logger.error(f"Error reading count data: {e}")
    else:
        current_status["counting"] = 0
        current_status["failed_counting"] = 0

    # 计算所有任务的总计数和失败总计数
    total_counting = 0
    total_failed_counting = 0

    # 遍历当前日期的所有计数文件
    for file in os.listdir(count_dir):
        if file.startswith(f"count_{formatted_date}_") and file.endswith(".json"):
            try:
                with open(os.path.join(count_dir, file), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    total_counting += data.get("counting", 0)
                    total_failed_counting += data.get("failed_counting", 0)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                g.logger.error(f"Error reading count data from {file}: {e}")

    # 将总计数添加到 current_status
    current_status["total_counting"] = total_counting
    current_status["total_failed_counting"] = total_failed_counting
    return current_status

class BlockingQueue:
    """
    自定义阻塞队列类，支持GPU数组
    - get方法：如果队列为空则阻塞等待
    - put方法：如果队列满了则移除最早的数据后插入新数据
    - get_with_index方法：消费时自动添加index信息
    - 支持GPU数组的内存管理
    """
    
    def __init__(self, maxsize=10, gpu_aware=True):
        self.maxsize = maxsize
        self.queue = deque()
        self.lock = threading.Lock()
        self.not_empty = threading.Condition(self.lock)
        self.gpu_aware = gpu_aware
        # 添加index计数器
        self.index_counter = 0
        self.index_max = 2400
    
    def put(self, item):
        """
        向队列中放入数据，支持GPU数组
        如果队列满了，移除最早的数据
        """
        with self.lock:
            # 如果队列满了，清理最早的数据
            if len(self.queue) >= self.maxsize:
                self.queue.popleft()
            # 添加新数据
            self.queue.append(item)
            
            # 通知等待的get方法
            self.not_empty.notify()
            
    
    def get(self):
        """
        从队列中获取数据
        如果队列为空则阻塞等待
        """
        with self.not_empty:
            # 等待直到队列不为空
            while len(self.queue) == 0:
                self.not_empty.wait()
            
            # 获取并返回数据
            return self.queue.popleft()
    
    def get_with_index(self):
        """
        从队列中获取数据并自动添加index
        如果队列为空则阻塞等待
        返回格式: {'frame': data, 'index': index}
        """
        with self.not_empty:
            # 等待直到队列不为空
            while len(self.queue) == 0:
                self.not_empty.wait()
            
            # 获取数据
            item = self.queue.popleft()
            
            # 为数据添加index
            current_index = self.index_counter
            self.index_counter = (self.index_counter + 1) % self.index_max
            
            return {
                'frame': item,
                'index': current_index
            }
    
    def full(self):
        """
        检查队列是否已满
        """
        with self.lock:
            return len(self.queue) >= self.maxsize
    
    def empty(self):
        """
        检查队列是否为空
        """
        with self.lock:
            return len(self.queue) == 0
    
    def qsize(self):
        """
        返回队列当前大小
        """
        with self.lock:
            return len(self.queue)
    
    def clear_queue(self):
        """清空队列并清理GPU内存"""
        with self.lock:
            if self.gpu_aware:
                while self.queue:
                    item = self.queue.popleft()
            else:
                self.queue.clear()


class SortedBlockingQueue:
    """
    按index排序的阻塞队列类
    - 数据按照index进行排序插入
    - 处理0-2399的循环计数场景
    """
    
    def __init__(self, maxsize=100):
        self.maxsize = maxsize
        self.queue = []  # 使用list来支持排序插入
        self.lock = threading.Lock()
        self.not_empty = threading.Condition(self.lock)
        self.index_max = 2400
    
    def _compare_index(self, index1, index2):
        """
        比较两个循环index的大小
        处理0-2399的循环场景
        """
        # 如果两个index都在同一个半圆内，直接比较
        if abs(index1 - index2) < self.index_max // 2:
            return index1 - index2
        else:
            # 处理跨越归零点的情况
            # 较小的数字实际上是较新的（归零后的）
            if index1 < index2:
                return 1  # index1 更大（更新）
            else:
                return -1  # index2 更大（更新）
    
    def put(self, item):
        """
        按index排序插入数据
        """
        with self.lock:
            index = item['index']
            
            # 如果队列为空，直接插入
            if not self.queue:
                self.queue.append(item)
            else:
                # 找到合适的插入位置
                inserted = False
                for i, existing_item in enumerate(self.queue):
                    if self._compare_index(index, existing_item['index']) < 0:
                        self.queue.insert(i, item)
                        inserted = True
                        break
                
                # 如果没有找到插入位置，说明应该插入到末尾
                if not inserted:
                    self.queue.append(item)
            
            # 如果队列超过最大大小，移除最后的元素
            if len(self.queue) > self.maxsize:
                self.queue.pop()
            
            # 通知等待的get方法
            self.not_empty.notify()
    
    def get(self):
        """
        获取队列中最小index的数据
        """
        with self.not_empty:
            # 等待直到队列不为空
            while len(self.queue) == 0:
                self.not_empty.wait()
            
            # 返回第一个元素（最小index）
            return self.queue.pop(0)
    
    def qsize(self):
        """
        返回队列当前大小
        """
        with self.lock:
            return len(self.queue)
    
    def empty(self):
        """
        检查队列是否为空
        """
        with self.lock:
            return len(self.queue) == 0
        
class LabelsSubscriber:
    """订阅 labels 的简易客户端。"""

    def __init__(
        self,
        endpoint: str,
        rcvhwm: int = 8,
        conflate: bool = False,  # 修复：默认禁用conflate，避免消息丢失
        verbose: bool = False,
    ):
        if zmq is None:
            raise RuntimeError("pyzmq 未安装，无法创建 LabelsSubscriber")

        self._ctx = zmq.Context()
        self._sock = self._ctx.socket(zmq.SUB)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.RCVHWM, rcvhwm)
        if conflate:
            try:
                self._sock.setsockopt(zmq.CONFLATE, 1)
            except Exception:
                pass

        self._verbose = verbose

        # **关键顺序**：先设置订阅，再 connect，降低慢启动期间丢消息的概率
        self._sock.setsockopt(zmq.SUBSCRIBE, b"labels")
        self._sock.connect(endpoint)

        # 初次连接后稍等片刻，给订阅规则传播到 PUB
        time.sleep(0.05)

    def recv(self, timeout_ms: int = 1000) -> Optional[List[List[int]]]:
        """接收一帧 labels；超时返回 None。"""
        try:
            evt = self._sock.poll(timeout=timeout_ms)
            if evt <= 0:
                return None
            topic, payload = self._sock.recv_multipart(flags=0)
            if topic != b"labels":
                return None
            return json.loads(payload.decode("utf-8"))
        except Exception as e:
            if self._verbose:
                print(f"[SUB] recv failed: {e}")
            return None

    def close(self):
        try:
            self._sock.close(0)
        except Exception:
            pass
        try:
            self._ctx.term()
        except Exception:
            pass


def throw_error(error_message):
    alarm_data = {
        "frpc_port": int(g.FRP_PORT),
        "content": error_message
    }
    

    response = requests.post(n.ISP_CLOUD_CONFIG['alarm_path'], headers={}, json=alarm_data)
    if response.status_code != 200:
        g.logger.error(f"报警失败: {response.text}")
    else:
        g.logger.info(f"报警成功: {response.text}")