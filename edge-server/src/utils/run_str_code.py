import src.core.config.nacos_var as n
import src.core.config.global_var as g
import time
import traceback
from src.core.database.sqlite_exec import update_count_data
from copy import deepcopy
from collections.abc import Iterable

STATUS_EMIT_INTERVAL = 0.1  # 100ms
last_status_emit_time = 0.0

update_status_runner = None
start_time = 0
end_time = 0
class PythonRunner:
    def __init__(self, python_code: str) -> None:
        self._globals = dict()
        self.load(python_code)

    def load(self, python_code: str):
        exec(python_code, self._globals)

    def run_fn(self, function_name, params):
        _function = self._globals.get(function_name)
        if not callable(_function):
            raise TypeError(
                f"Object: {function_name} is not a function. Globals: {self._globals}"
            )
        if isinstance(params, dict):
            return _function(**params)
        elif isinstance(params, Iterable):
            return _function(*params)
        else:
            return _function(params)


def execute_update_status(labels):
    """执行 PY_CODE 中的 update_status 函数"""
    global update_status_runner, start_time, end_time
    cur_time = int(time.time())
    if start_time == 0:
        start_time = cur_time
    args = {
        "bom_en": g.LABEL_BOM_EN,
        "process_status": n.PROCESS_STATUS,
        "bom_thresholds":  n.BOM_DICT
    }

    update_status_ret = None
    try:
        if update_status_runner is None:
            update_status_runner = PythonRunner(n.PY_CODE)
        update_status_ret = update_status_runner.run_fn('update_status', [labels, args])
            
    except Exception as e:
        g.logger.error(f"执行 PY_CODE 时出错：{e}")
        g.logger.error(f"错误堆栈：\n{traceback.format_exc()}")
        return None
    if update_status_ret:
        if update_status_ret['alert_pre_status']:
            update_count_data(n.PROCESS_STATUS['current_task']['task_id'], 
                n.PROCESS_STATUS['current_status']['counting'], n.PROCESS_STATUS['current_status']['failed_counting'])
            end_time = cur_time
            # 确保时间顺序正确，避免 start_time > end_time
            if start_time > end_time:
                start_time = end_time - n.VIDEO_CONFIG['slice_seconds']
            # 计算 TT 时间（秒）
            tt_time = int(end_time - start_time)
            # 发送 TT 时间到前端
            g.SOCKET_IO.emit('tt', tt_time)
            produce_data = {
                "start_time": start_time,
                "end_time": end_time,
                "task_id": n.PROCESS_STATUS['current_task']['task_id'],
                "update_status_ret": update_status_ret['alert_pre_status']
            }
            g.PRODUCE_DATA_QUEUE.put(deepcopy(produce_data))
            # 重置开始时间为下一个周期的开始
            start_time = cur_time
        global last_status_emit_time
        n.PROCESS_STATUS = update_status_ret['process_status']
        now = time.time()
        if now - last_status_emit_time >= STATUS_EMIT_INTERVAL:
            g.SOCKET_IO.emit('status', n.PROCESS_STATUS['current_status'])
            last_status_emit_time = now
