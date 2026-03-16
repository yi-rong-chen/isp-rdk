import time
import os
from typing import Any, Dict, Optional
from collections import OrderedDict
import src.core.config.global_var as g

import requests
from casdoor import CasdoorSDK
import src.core.config.nacos_var as n
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

TOKEN_EXPIRED_CODE = 4031

class LRUCache:
    def __init__(self, max_size=100, ttl=3600):  # 1小时TTL
        self.max_size = max_size
        self.ttl = ttl
        self.cache = OrderedDict()
        self.timestamps = {}
    
    def get(self, key):
        if key not in self.cache:
            return None
        if time.time() - self.timestamps[key] > self.ttl:
            del self.cache[key]
            del self.timestamps[key]
            return None
        self.cache.move_to_end(key)
        return self.cache[key]
    
    def put(self, key, value):
        if len(self.cache) >= self.max_size:
            oldest = next(iter(self.cache))
            del self.cache[oldest]
            del self.timestamps[oldest]
        self.cache[key] = value
        self.timestamps[key] = time.time()

_IN_MEMORY_CACHE_FOR_TOKEN_ = LRUCache()
_IN_MEMORY_CACHE_FOR_META_ = LRUCache()


def _make_in_memory_cache_key_for_token(
    org_name: str, application_name: str, username: str
) -> str:
    return f"token-{org_name}-{application_name}-{username}"


def _make_in_memory_cache_key_for_meta(org_name: str, application_name: str) -> str:
    return f"meta-{org_name}-{application_name}"


class ThrottledFile:
    """限速文件读取器，用于控制上传速度"""
    def __init__(self, file_obj, max_speed_bytes_per_sec: Optional[int] = None):
        """
        :param file_obj: 文件对象
        :param max_speed_bytes_per_sec: 最大上传速度（字节/秒），None表示不限速
        """
        self.file_obj = file_obj
        self.max_speed = max_speed_bytes_per_sec
        self.start_time = time.time()
        self.bytes_read = 0
        self.chunk_size = 64 * 1024  # 64KB 块大小
        
    def read(self, size=-1):
        if self.max_speed is None:
            return self.file_obj.read(size)
        
        # 如果size为-1，读取整个文件，但需要限速
        if size == -1:
            size = self.chunk_size
        
        # 计算当前时间
        current_time = time.time()
        elapsed = current_time - self.start_time
        
        # 计算允许读取的字节数
        if elapsed > 0:
            allowed_bytes = int(self.max_speed * elapsed)
            if self.bytes_read >= allowed_bytes:
                # 需要等待以达到限速
                sleep_time = (self.bytes_read - allowed_bytes) / self.max_speed
                if sleep_time > 0.001:  # 只等待超过1ms的时间
                    time.sleep(sleep_time)
                    self.start_time = time.time()
                    self.bytes_read = 0
                    elapsed = 0.001  # 重置后给一个小的初始值
                else:
                    elapsed = 0.001
            else:
                # 计算剩余配额
                remaining_quota = allowed_bytes - self.bytes_read
                if remaining_quota <= 0:
                    # 配额用完，需要等待
                    time.sleep(0.01)
                    self.start_time = time.time()
                    self.bytes_read = 0
                    remaining_quota = int(self.max_speed * 0.01)
                
                # 限制读取大小不超过配额
                if size > 0:
                    read_size = min(size, remaining_quota)
                else:
                    read_size = remaining_quota
        else:
            # 初始状态，可以读取一个小的块
            read_size = min(size if size > 0 else self.chunk_size, int(self.max_speed * 0.1))
        
        data = self.file_obj.read(read_size)
        self.bytes_read += len(data)
        return data
    
    def __getattr__(self, name):
        return getattr(self.file_obj, name)
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        if hasattr(self.file_obj, 'close'):
            self.file_obj.close()


class Request:
    def __init__(
        self, org_name: str, application_name: str, username: str, password: str
    ):
        self.org_name = org_name
        self.application_name = application_name
        self.username = username
        self.password = password
        self.casdoor_sdk = self._get_casdoor_sdk()
        self.casdoor_access_token = self._get_casdoor_token()

    def _get_casdoor_sdk(self) -> CasdoorSDK:
        meta = self._get_casdoor_meta()
        
        casdoor_sdk = CasdoorSDK(
            endpoint=meta.get("endpoint"),
            client_id=meta.get("client_id"),
            client_secret=meta.get("client_secret"),
            certificate=meta.get("certificate"),
            org_name=self.org_name,
            application_name=meta.get("application_name"),
        )
        return casdoor_sdk

    def _get_casdoor_meta(self) -> Dict[str, Any]:
        key = _make_in_memory_cache_key_for_meta(
            org_name=self.org_name, application_name=self.application_name
        )

        val = _IN_MEMORY_CACHE_FOR_META_.get(key)
        if val is not None:
            return val

        apollo_config_url = "http://69.230.223.248:9080/configs"
        app_id = "auth"
        cluster = "dev"
        namespace = f"{self.application_name}_{self.org_name}"
        url = f"{apollo_config_url}/{app_id}/{cluster}/{namespace}"
        
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()  # 检查HTTP错误
            config_data = response.json().get("configurations", {})
            _IN_MEMORY_CACHE_FOR_META_.put(key, config_data)
            return config_data
        except requests.RequestException as e:
            g.logger.error(f"获取配置失败: {e}")
            return {}

    def _get_casdoor_token(self) -> str:
        key = _make_in_memory_cache_key_for_token(
            org_name=self.org_name,
            application_name=self.application_name,
            username=self.username,
        )
        val = _IN_MEMORY_CACHE_FOR_TOKEN_.get(key)
        if val is not None:
            return val
        oauth_token = self.casdoor_sdk.get_oauth_token(
            username=self.username,
            password=self.password,
        )
        _IN_MEMORY_CACHE_FOR_TOKEN_.put(key, oauth_token.get("access_token"))
        return oauth_token.get("access_token")

    def _update_casdoor_token(self):
        oauth_token = self.casdoor_sdk.get_oauth_token(
            username=self.username, password=self.password
        )
        self.casdoor_access_token = oauth_token.get("access_token")
        key = _make_in_memory_cache_key_for_token(
            org_name=self.org_name,
            application_name=self.application_name,
            username=self.username,
        )
        _IN_MEMORY_CACHE_FOR_TOKEN_.put(key, self.casdoor_access_token)

    def request(self, method, url, max_upload_speed: Optional[int] = None, **kwargs) -> requests.Response:
        """
        :param method: HTTP方法
        :param url: 请求URL
        :param max_upload_speed: 最大上传速度（字节/秒），None表示不限速
        :param kwargs: 其他requests参数
        :return: requests.Response
        """
        headers = kwargs.get("headers", {})
        kwargs["headers"] = headers
        
        # 添加Casdoor认证头
        headers["Casdoor-Org-Name"] = self.org_name
        headers["Casdoor-Application-Name"] = self.application_name
        headers["Casdoor-Access-Token"] = self.casdoor_access_token
        
        # 保存原始的files和data，用于token过期时重试
        original_files = kwargs.get("files")
        original_data = kwargs.get("data")
        use_throttled_upload = max_upload_speed and method.upper() == "POST" and original_files
        
        # 如果有限速且是POST请求且有files参数，使用限速上传
        if use_throttled_upload:
            # 使用MultipartEncoder进行限速上传
            fields = {}
            
            # 添加data字段
            if original_data:
                for key, value in original_data.items():
                    fields[key] = value
            
            # 添加files字段，使用限速文件读取器
            throttled_files = {}
            for key, file_obj in original_files.items():
                if hasattr(file_obj, 'read'):
                    # 如果是文件对象，包装为限速文件
                    file_name = os.path.basename(getattr(file_obj, 'name', key))
                    throttled_files[key] = (file_name, ThrottledFile(file_obj, max_upload_speed))
                else:
                    throttled_files[key] = file_obj
            
            fields.update(throttled_files)
            
            # 创建MultipartEncoder
            encoder = MultipartEncoder(fields=fields)
            headers["Content-Type"] = encoder.content_type
            
            # 移除files和data参数，使用data参数传递encoder
            kwargs.pop("files", None)
            kwargs.pop("data", None)
            kwargs["data"] = encoder
            
            # 调用requests.request
            resp = requests.request(method=method, url=url, **kwargs)
        else:
            # 普通请求，不限速
            resp = requests.request(method=method, url=url, **kwargs)
        
        try:
            if resp.json().get("code") == TOKEN_EXPIRED_CODE:
                self._update_casdoor_token()
                headers["Casdoor-Access-Token"] = self.casdoor_access_token
                # 重新发送请求（如果之前使用了限速，需要重新构建）
                if use_throttled_upload:
                    # 尝试将文件对象重新定位到开头（如果支持）
                    for key, file_obj in original_files.items():
                        if hasattr(file_obj, 'seek') and hasattr(file_obj, 'tell'):
                            try:
                                file_obj.seek(0)
                            except (IOError, OSError):
                                # 如果无法重新定位，记录警告但继续
                                g.logger.warning(f"无法重新定位文件对象 {key}，可能影响重试")
                    
                    # 重新构建限速上传
                    fields = {}
                    if original_data:
                        for key, value in original_data.items():
                            fields[key] = value
                    throttled_files = {}
                    for key, file_obj in original_files.items():
                        if hasattr(file_obj, 'read'):
                            file_name = os.path.basename(getattr(file_obj, 'name', key))
                            throttled_files[key] = (file_name, ThrottledFile(file_obj, max_upload_speed))
                        else:
                            throttled_files[key] = file_obj
                    fields.update(throttled_files)
                    encoder = MultipartEncoder(fields=fields)
                    headers["Content-Type"] = encoder.content_type
                    kwargs.pop("files", None)
                    kwargs.pop("data", None)
                    kwargs["data"] = encoder
                resp = requests.request(method=method, url=url, **kwargs)
        except:
            pass
        return resp

    def request_with_on_error_callback(
        self,
        on_error_callback_func: Any,
        on_error_callback_args: Any,
        retry_max_times: int = 3,
        retry_interval: int = 1,
        *args,
        **kwargs,
    ) -> requests.Response:
        """
        a wrapper for official ``requests.request``, supports multi-tenant and on-error callback

        :param on_error_callback_func: callback function when error occurs
        :param on_error_callback_args: callback function params, eg: (1, "param_x")
        :param retry_max_times: maximum retry times
        :param retry_interval: wait interval between two retries, unit is in seconds
        :return: ``requests.Response``

        Usage::
            >>> from ccai_multitenant.httprequest.core import Request
            >>> request = Request(org_name, application_name, username, password)
            >>> request.request_with_on_error_callback(
                    on_error_callback_func=<>
                    on_error_callback_args=<>
                    retry_max_times=<>,
                    retry_interval=<>,
                    method=<>,
                    url=<>,
                    json=<>,
                )
            <Response [200]>
        """
        retry_times = 0
        while retry_times < retry_max_times:
            retry_times += 1
            success = True
            try:
                resp = self.request(*args, **kwargs)
            except Exception as e:
                success = False
                g.logger.error(f"request failed, retry_time = {retry_times}")
                if retry_times == retry_max_times:
                    g.logger.error(
                        f"reach retry max times, trigger on-error-callback = {on_error_callback_func}"
                    )
                    on_error_callback_func(*on_error_callback_args)
                    raise e
            if success:
                return resp
            time.sleep(retry_interval)

def post(url, params=None, data=None, json_data=None, files=None, timeout=60, max_upload_speed=None):
    """
    :param url: 请求URL
    :param params: URL参数
    :param data: 表单数据
    :param json_data: JSON数据
    :param files: 文件字典
    :param timeout: 超时时间（秒）
    :param max_upload_speed: 最大上传速度（字节/秒），例如 1024*1024 表示1MB/s，None表示不限速
    :return: requests.Response
    """
    if params is None:
        params = {}
    if data is None:
        data = {}
    if json_data is None:
        json_data = {}
    if files is None:
        files = {}
        
    auth_config = n.CASSDOR_AUTH_CONFIG
    request = Request(
        org_name=auth_config['org_name'],
        application_name=auth_config['app_name'],
        username=auth_config['user_name'],
        password=auth_config['password']
    )

    return request.request(
        method="POST",
        url=url,
        params=params,
        data=data,
        json=json_data,
        files=files,
        timeout=timeout,
        max_upload_speed=max_upload_speed
    )

def get(url, params=None, data=None, json_data=None, files=None, timeout=60):
    if params is None:
        params = {}
    if data is None:
        data = {}
    if json_data is None:
        json_data = {}
    if files is None:
        files = {}
        
    auth_config = n.CASSDOR_AUTH_CONFIG
    request = Request(
        org_name=auth_config['org_name'],
        application_name=auth_config['app_name'],
        username=auth_config['user_name'],
        password=auth_config['password']
    )
    
    return request.request(
        method="GET",
        url=url,
        params=params,
        data=data,
        json=json_data,
        files=files,
        timeout=timeout
    )