import time
import traceback
from datetime import datetime
import src.core.config.global_var as g
import src.core.config.nacos_var as n
from src.utils.run_str_code import execute_update_status

LAST_ALARM_TIME = 0
LAST_CHECK_DATE = None  # 用于跟踪上次检查的日期

def check_and_reset_daily_counters():
    """检查是否跨天，如果跨天则重置计数字段"""
    global LAST_CHECK_DATE
    current_date = datetime.now().date()
    # 如果是第一次运行或者日期发生了变化
    if LAST_CHECK_DATE is None or LAST_CHECK_DATE != current_date:
        if LAST_CHECK_DATE is not None:  # 不是第一次运行，说明确实跨天了
            g.logger.info(f"检测到跨天：从 {LAST_CHECK_DATE} 到 {current_date}，重置计数字段")
            
            # 重置 current_status 中的4个计数字段
            if 'current_status' in n.PROCESS_STATUS:
                n.PROCESS_STATUS['current_status']['counting'] = 0
                n.PROCESS_STATUS['current_status']['total_counting'] = 0
                n.PROCESS_STATUS['current_status']['failed_counting'] = 0
                n.PROCESS_STATUS['current_status']['total_failed_counting'] = 0
                
                g.logger.info("已重置计数字段：counting, total_counting, failed_counting, total_failed_counting")
        
        # 更新最后检查的日期
        LAST_CHECK_DATE = current_date

def status_update_loop():
    """按顺序处理状态更新的线程"""
    global LAST_ALARM_TIME
    
    # 检查IPC订阅端是否可用
    if g.LABELS_SUBSCRIBER is None:
        g.logger.error("IPC订阅端未初始化，状态更新线程退出")
        return
    
    while True:
        try:
            check_and_reset_daily_counters()
            current_time = int(time.time())
            ng_time = n.TRICOLOUR_LIGHT_STATUS.get('ng_time', 3)
            if n.TRICOLOUR_LIGHT_FLAG and (LAST_ALARM_TIME > 0) and (LAST_ALARM_TIME < (current_time - ng_time)):
                if g.TRICOLOUR_LIGHT_CLIENT is not None:
                    running_status = n.TRICOLOUR_LIGHT_STATUS.get('running_status', {})
                    g.TRICOLOUR_LIGHT_CLIENT.set_light(running_status.get('running', 'yellow'))
                    g.ALARM_STATUS = False
                LAST_ALARM_TIME = 0
            
            # 使用超时接收，避免无限阻塞
            status_data = g.LABELS_SUBSCRIBER.recv(timeout_ms=1000)  # 1秒超时
            if status_data is None:
                # 没有数据时短暂休眠，避免CPU占用过高
                time.sleep(0.1)
                continue
            g.ALARM_CONFIG["model_call_failed_timestamp"] = current_time
            if g.APP_STATUS == g.AppStatus.RUNNING:
                execute_update_status(status_data)

        except Exception as e:
            g.logger.error(f"状态更新线程异常: {e}")
            traceback.print_exc()
            # 异常时休眠，避免快速重试
            time.sleep(1)
