import os
import json
import requests
import src.core.config.global_var as g
import ast
from src.utils.tool import throw_error
from src.hardware.tricolour_light import create_tricolour_light

BOM_DICT = {}

PY_CODE = """
def update_status(labels, args):
    return {
        'status': None,
        'alert_pre_status': None,
        'process_status': None,
        'change_flag': None,
        'alter_status': None
    }
"""

DEV_FLAG = True

VIDEO_CONFIG = {
    "segment_seconds": 60,
    "slice_seconds": 20,
    "bitrate_coefficient": 1
}

CAMERA_CONFIG = {}

PROCESS_STATUS = {}

ISP_CLOUD_CONFIG = {
    "create_task_path": "https://api.caochuan.cc/isp_server/api/isp/task/edge_create",
    "task_list_query_path": "https://api.caochuan.cc/isp_server/api/isp/task/edge_task_list",
    "upload_record_cloud_path": "http://52.83.251.142:7086/api/isp/video/upload_product_info_and_video",
    "upload_log_cloud_path": "https://api.caochuan.cc/isp_server/api/isp/record/create",
    "upload_failed_video_path": "https://api.caochuan.cc/isp_server/api/isp/video/ng_video_split_task",
    "alarm_path": "https://api.caochuan.cc/isp_server/api/isp/common/alarm/feishu",
    "cur_task_id": "0"
}

CASSDOR_AUTH_CONFIG = {
    "org_name": "",
    "app_name": "isp",
    "user_name": "_admin",
    "password": "Changeme_123"
}

TRICOLOUR_LIGHT_FLAG = False

TRICOLOUR_LIGHT_STATUS = {
    "running_status": {
        "idle": "yellow",
        "running": "yellow",
        "ok": "green",
        "ng": "red,buzzer"
    },
    "ng_time": 3
}

FINISH_HOOK_CODE = """
def finish_hook_code(task_data):
    return None
"""

LABELS_CONFS = {}

WORKSTATION_NAME = ""

UPLOAD_SUCCESS = False


def normalize_camera_config(camera_config):
    if not isinstance(camera_config, dict):
        return {}

    mipi_config = camera_config.get("mipi")
    if isinstance(mipi_config, dict):
        return mipi_config

    normalized_config = dict(camera_config)
    normalized_config.pop("type", None)
    return normalized_config


def normalize_labels_confs(labels_confs):
    if not isinstance(labels_confs, dict):
        return {}

    normalized_confs = {}
    for raw_label, raw_conf in labels_confs.items():
        label = str(raw_label).strip()
        if not label:
            continue

        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            g.logger.warning(f"忽略无效的 LABELS_CONFS 配置: label={raw_label}, conf={raw_conf}")
            continue

        normalized_confs[label] = max(0.0, min(1.0, conf))

    return normalized_confs

def fetch_and_save_config(frp_port):
    # 检查isp.json是否已存在
    if os.path.exists('isp.json'):
        g.logger.info("isp.json文件已存在，跳过配置获取")
        return

    workstation_index = int(getattr(g, "WORKSTATION_INDEX", 0) or 0)
    # 准备请求数据
    payload = {
        "frp_port": int(frp_port),
        "station_id": workstation_index,
    }
    headers = {
        "Content-Type": "application/json"
    }

    try:
        g.logger.info(f"正在从 {g.API_URL} 拉取配置，station_id={workstation_index} ...")
        response = requests.post(g.API_URL, headers=headers, json=payload)

        if response.status_code == 200:
            response_data = response.json()
            configurations = response_data.get('message', {})

            if not configurations:
                g.logger.info("警告: 返回的配置为空")
                return

            # 写入到isp.json文件
            with open('isp.json', 'w', encoding='utf-8') as f:
                json.dump(configurations, f, ensure_ascii=False, indent=2)

            g.logger.info(f"配置已成功写入到 {os.path.abspath('isp.json')}")
            g.logger.info(f"共写入 {len(configurations)} 个配置项")
        else:
            g.logger.error(f"错误: 获取配置失败 (HTTP {response.status_code})")
            g.logger.error(f"响应内容: {response.text}")
            throw_error(f"获取isp配置失败 (HTTP {response.status_code})")
            raise Exception(f"获取isp配置失败 (HTTP {response.status_code})")
    except Exception as e:
        g.logger.error(f"错误: {str(e)}")
        throw_error(f"获取isp配置失败: {str(e)}")
        raise Exception(f"获取isp配置失败 (HTTP {response.status_code})")

    
def parse_isp_json(file_path='isp.json'):
    if not os.path.exists(file_path):
        throw_error(f"配置文件 {file_path} 不存在")
        raise FileNotFoundError(f"配置文件 {file_path} 不存在")

    try:
        # 读取JSON文件
        with open(file_path, 'r', encoding='utf-8') as f:
            config_json = json.load(f)

        # 解析每个值为对应的Python类型
        parsed_config = {}
        for key, value in config_json.items():
            parsed_value = parse_value(value)
            parsed_config[key] = parsed_value

        return parsed_config

    except json.JSONDecodeError:
        throw_error(f"文件 {file_path} 不是有效的JSON格式")
        raise ValueError(f"文件 {file_path} 不是有效的JSON格式")
    except Exception as e:
        throw_error(f"解析配置文件时出错: {str(e)}")
        raise Exception(f"解析配置文件时出错: {str(e)}")


def parse_value(value):
    if not isinstance(value, str):
        return value

    # 尝试解析为JSON
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass

    # 尝试解析为Python字面量
    try:
        # 处理None、True、False等字面量
        if value.strip() == 'None':
            return None
        elif value.strip() == 'True':
            return True
        elif value.strip() == 'False':
            return False

        # 处理数字
        try:
            if '.' in value:
                return float(value)
            else:
                return int(value)
        except ValueError:
            pass

        # 处理Python表达式
        try:
            # 注意：ast.literal_eval只解析字面量，比eval更安全
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            pass

        # 处理多行字符串中的Python代码
        if value.startswith("'''") and value.endswith("'''"):
            return value[3:-3]
    except Exception:
        pass

    # 如果无法解析，则保持原始字符串
    return value

def check_is_in_nacos(nacos_dict, key):
    if nacos_dict == {}:
        throw_error("nacos配置为空")
        raise Exception("nacos配置为空")
    if key not in nacos_dict:
        throw_error(f"{key} 配置为空")
        raise Exception(f"{key} 配置为空")

def init_nacos_var():
    global BOM_DICT
    global PY_CODE
    global DEV_FLAG
    global CAMERA_CONFIG
    global PROCESS_STATUS
    global ISP_CLOUD_CONFIG
    global CASSDOR_AUTH_CONFIG
    global TRICOLOUR_LIGHT_FLAG
    global VIDEO_CONFIG
    global TRICOLOUR_LIGHT_STATUS
    global FINISH_HOOK_CODE
    global LABELS_CONFS
    global WORKSTATION_NAME
    global UPLOAD_SUCCESS

    fetch_and_save_config(g.FRP_PORT)
    
    nacos_dict = parse_isp_json()
    
    check_is_in_nacos(nacos_dict, 'BOM_DICT')
    BOM_DICT = nacos_dict.get('BOM_DICT')

    check_is_in_nacos(nacos_dict, 'PY_CODE')
    PY_CODE = nacos_dict.get('PY_CODE')

    check_is_in_nacos(nacos_dict, 'DEV_FLAG')
    DEV_FLAG = nacos_dict.get('DEV_FLAG')

    check_is_in_nacos(nacos_dict, 'CAMERA_CONFIG')
    CAMERA_CONFIG = normalize_camera_config(nacos_dict.get('CAMERA_CONFIG'))

    check_is_in_nacos(nacos_dict, 'VIDEO_CONFIG')
    VIDEO_CONFIG = nacos_dict.get('VIDEO_CONFIG')

    check_is_in_nacos(nacos_dict, 'PROCESS_STATUS')
    PROCESS_STATUS = nacos_dict.get('PROCESS_STATUS')

    check_is_in_nacos(nacos_dict, 'ISP_CLOUD_CONFIG')
    ISP_CLOUD_CONFIG = nacos_dict.get('ISP_CLOUD_CONFIG')  


    check_is_in_nacos(nacos_dict, 'CASSDOR_AUTH_CONFIG')
    CASSDOR_AUTH_CONFIG = nacos_dict.get('CASSDOR_AUTH_CONFIG')

    check_is_in_nacos(nacos_dict, 'TRICOLOUR_LIGHT_FLAG')
    TRICOLOUR_LIGHT_FLAG = nacos_dict.get('TRICOLOUR_LIGHT_FLAG')

    check_is_in_nacos(nacos_dict, 'TRICOLOUR_LIGHT_STATUS')
    TRICOLOUR_LIGHT_STATUS = nacos_dict.get('TRICOLOUR_LIGHT_STATUS')
    
    check_is_in_nacos(nacos_dict, 'LABELS_CONFS')
    LABELS_CONFS = normalize_labels_confs(nacos_dict.get('LABELS_CONFS'))
    
    # 如果三色灯开关打开，则初始化三色灯
    if TRICOLOUR_LIGHT_FLAG:
        g.TRICOLOUR_LIGHT_CLIENT = create_tricolour_light()
        if g.TRICOLOUR_LIGHT_CLIENT is not None:
            running_status = TRICOLOUR_LIGHT_STATUS.get('running_status', {})
            idle_color = running_status.get('idle', 'yellow')
            g.TRICOLOUR_LIGHT_CLIENT.set_light(idle_color)
            g.logger.info("三色灯初始化成功，类型: usb")
        else:
            g.logger.warning("三色灯初始化失败，类型: usb")
    else:
        g.TRICOLOUR_LIGHT_CLIENT = None

    check_is_in_nacos(nacos_dict, 'WORKSTATION_NAME')
    WORKSTATION_NAME = nacos_dict.get('WORKSTATION_NAME')
    
    check_is_in_nacos(nacos_dict, 'UPLOAD_SUCCESS')
    UPLOAD_SUCCESS = nacos_dict.get('UPLOAD_SUCCESS')
