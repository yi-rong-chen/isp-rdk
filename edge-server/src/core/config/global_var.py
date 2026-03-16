# 全局配置和变量定义
from enum import Enum
import time
import configparser
import src.core.config.nacos_var as n
from src.utils.tool import LabelsSubscriber
from src.processing.rdk_manager import RDKManager
import os

# 应用状态枚举
class AppStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"

# 当前应用状态
APP_STATUS = AppStatus.IDLE

# 模型调用接口
MODEL_URL = "http://192.169.1.26:7777/_predict"

NG_VIDEO_SLICE = {
    "start_time": 0,
    "end_time": 0
}

ALARM_CONFIG = {
    "model_call_failed_timestamp": 0,
    "failed_flag": False
}

FRP_PORT = 0

API_URL = "http://52.83.251.142:8070/frp_manager/read_workstation_content"

# 队列将在初始化函数中创建，避免循环导入
PRODUCE_DATA_QUEUE = None

BOM_EN = []

BOM_ZH = []

LABEL_BOM_EN = []

SOCKET_IO = None

IO_STATUS = None

# 端口配置
FINISH_API_PORT = 5050
ISP_PORT = 9090

LABELS_SUBSCRIBER = None

FRONTEND_REGISTRY_URL = 'http://192.169.1.10:3001'

# 创建RDK管理器实例
RDK_MANAGER = None

# ERROR_STATUS
ERROR_STATUS = False

TRICOLOUR_LIGHT_CLIENT = None

START_FLAG = False

ALARM_STATUS = False

DISAPPEAR_LINE = None

WORKSTATION_INDEX = 0

PROCESS_NAME = ""

# 日志配置 - 使用统一的日志系统
def setup_logger():
    """配置日志系统 - 使用统一的日志配置"""
    # 使用统一的日志配置，指定log_name为"app"
    from rdk.logger_config import get_logger
    return get_logger('app_logger', log_name='app')

# 全局logger实例
logger = setup_logger()

file_path = '/usr/local/frp/frpc.ini'
config = configparser.ConfigParser()

try:
    config.read(file_path)
    logger.info(f"成功读取配置文件: {file_path}")
    
    for section in config.sections():
        if section.startswith('ssh_'):
            if 'remote_port' in config[section]:
                FRP_PORT = config[section]['remote_port']
                logger.info(f"设置FRP端口: {FRP_PORT}")
                break
except Exception as e:
    logger.error(f"读取配置文件失败: {e}")

def init_global_var():
    global BOM_EN
    global BOM_ZH
    global LABEL_BOM_EN
    global FRP_PORT
    global NG_VIDEO_SLICE
    global ALARM_CONFIG
    global PRODUCE_DATA_QUEUE
    global FINISH_API_PORT
    global ISP_PORT
    global LABELS_SUBSCRIBER
    global RDK_MANAGER
    global WORKSTATION_INDEX
    global PROCESS_NAME

    logger.info("开始初始化全局变量")

    PROCESS_NAME = os.path.basename(os.getcwd()) or "edge-server"
    logger.info(f"当前实例名称: {PROCESS_NAME}, WORKSTATION_INDEX: {WORKSTATION_INDEX}")

    # 初始化队列，避免循环导入
    from src.utils.tool import BlockingQueue
    PRODUCE_DATA_QUEUE = BlockingQueue(maxsize=50)  # 从100减少到50

    current_time = int(time.time())
    logger.info(f"当前时间戳: {current_time}")

    NG_VIDEO_SLICE["start_time"] = current_time
    ALARM_CONFIG["model_call_failed_timestamp"] = current_time
    
    # 单工位部署，端口使用固定值
    FINISH_API_PORT = 5050
    ISP_PORT = 9090
    
    RDK_MANAGER = RDKManager()
    RDK_MANAGER.start_rdk()
    logger.info(
        f"设置端口配置 - FINISH_API_PORT: {FINISH_API_PORT}, ISP_PORT: {ISP_PORT}"
    )
    try:
        LABELS_SUBSCRIBER = LabelsSubscriber(endpoint=f"ipc:///tmp/{FINISH_API_PORT}.ipc", conflate=False, verbose=False)
        logger.info("IPC订阅端初始化成功")
    except Exception as e:
        logger.error(f"IPC订阅端初始化失败: {e}")
        LABELS_SUBSCRIBER = None
    
    BOM_EN = list(n.BOM_DICT.keys())
    
    BOM_ZH = [val["name"] for val in n.BOM_DICT.values()]

    LABEL_BOM_EN = list(n.LABELS_CONFS.keys())
