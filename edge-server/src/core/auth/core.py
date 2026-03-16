import time
from typing import Any, Dict
from collections import OrderedDict
import src.core.config.global_var as g

import requests
from casdoor import CasdoorSDK

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

    def request(self, method, url, **kwargs) -> requests.Response:
        headers = kwargs.get("headers", {})
        kwargs["headers"] = headers
        
        # 添加Casdoor认证头
        headers["Casdoor-Org-Name"] = self.org_name
        headers["Casdoor-Application-Name"] = self.application_name
        headers["Casdoor-Access-Token"] = self.casdoor_access_token
        
        # 调用requests.request时使用正确的参数顺序
        resp = requests.request(method=method, url=url, **kwargs)
        
        try:
            if resp.json().get("code") == TOKEN_EXPIRED_CODE:
                self._update_casdoor_token()
                headers["Casdoor-Access-Token"] = self.casdoor_access_token
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
