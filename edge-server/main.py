from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import src.core.config.global_var as g
import src.core.config.nacos_var as n
import json
import src.core.auth.casdoor_request as casdoor_request
from src.processing.work_thread import launch_thread
from src.core.database.sqlite_exec import read_count_by_task_id, init_sqlite, write_tasks_content, write_current_task_id, read_current_task_id, read_tasks_content
from flask_socketio import SocketIO
from src.utils.tool import throw_error
import datetime
# 三色灯接口已通过全局变量 g.TRICOLOUR_LIGHT_CLIENT 提供
from src.hardware.device_binding import verify_device
import os
import requests
import threading
import time

n.init_nacos_var()
g.init_global_var()
init_sqlite()

# 全局变量：记录上次重启 RDK 推理服务的时间
last_restart_time = 0
STARTUP_CHECK_EVENT = "startup_check"
STARTUP_CHECK_FAILURES = []
STARTUP_CHECK_LOCK = threading.Lock()
DEVICE_AUTHORIZED = True

app = Flask(__name__)

# 优化Flask配置
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB限制
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # 禁用缓存
#
# Flask 默认会对 jsonify 输出的 dict key 进行排序（JSON_SORT_KEYS=True），
# 会导致 task.detail 等字段的顺序被按字母序重排，从而与数据库/上游返回的插入顺序不一致。
# 关闭后将保留 Python dict 的插入顺序（Python 3.7+ 保序）。
app.config['JSON_SORT_KEYS'] = False
try:
    # Flask 2.2+：显式关闭 JSON provider 的排序（比仅配 config 更稳）
    app.json.sort_keys = False
except Exception:
    pass

# 添加CORS支持
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})


def _extract_request_host() -> str:
    host_header = request.headers.get("X-Forwarded-Host") or request.host or ""
    host = host_header.split(",", 1)[0].strip()
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host or request.remote_addr or "127.0.0.1"


def _build_runtime_urls() -> dict:
    host = _extract_request_host()
    return {
        "api_url": f"http://{host}:{g.ISP_PORT}",
        "stream_url": f"http://{host}:{g.FINISH_API_PORT}/stream",
    }


def _build_startup_check_payload(check_item: str, message: str, level: str = "warning", extra: dict = None):
    payload = {
        "check_item": check_item,
        "status": "failed",
        "level": level,
        "message": message,
        "timestamp": datetime.datetime.now().isoformat()
    }
    if extra:
        payload.update(extra)
    return payload


def _emit_startup_check(payload: dict, sid: str = None):
    if g.SOCKET_IO is None:
        return
    try:
        if sid:
            g.SOCKET_IO.emit(STARTUP_CHECK_EVENT, payload, room=sid)
        else:
            g.SOCKET_IO.emit(STARTUP_CHECK_EVENT, payload)
    except Exception as e:
        g.logger.error(f"发送启动自检消息失败: {e}")


def _notify_startup_check_failure(payload: dict):
    with STARTUP_CHECK_LOCK:
        exists = any(item.get("check_item") == payload.get("check_item") for item in STARTUP_CHECK_FAILURES)
        if not exists:
            STARTUP_CHECK_FAILURES.append(payload)
    _emit_startup_check(payload)


def _emit_cached_startup_check_failures_to_client(sid: str):
    with STARTUP_CHECK_LOCK:
        cached_failures = list(STARTUP_CHECK_FAILURES)
    for payload in cached_failures:
        _emit_startup_check(payload, sid=sid)


def run_startup_self_checks():
    global DEVICE_AUTHORIZED

    g.logger.info("开始执行服务启动自检")

    try:
        DEVICE_AUTHORIZED = bool(verify_device())
    except Exception as e:
        DEVICE_AUTHORIZED = False
        message = f"设备授权校验异常: {e}"
        g.logger.error(message)
        _notify_startup_check_failure(
            _build_startup_check_payload(
                check_item="device_authorization",
                message=message,
                level="error"
            )
        )
        # 按检测顺序仅推送首个失败告警
        return

    if DEVICE_AUTHORIZED:
        g.logger.info("设备授权自检通过")
    else:
        message = "设备未授权，请检查 device.json 中的授权设备ID配置"
        g.logger.error(message)
        _notify_startup_check_failure(
            _build_startup_check_payload(
                check_item="device_authorization",
                message=message,
                level="error"
            )
        )
        # 按检测顺序仅推送首个失败告警
        return

    g.logger.info("当前设备固定使用 MIPI 相机，跳过相机网络连通性自检")

# 请求日志中间件
@app.before_request
def log_request_info():
    # 获取请求信息
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    method = request.method
    path = request.path
    args = dict(request.args)
    headers = dict(request.headers)
    body = request.get_data()
    
    # 记录请求日志
    g.logger.info(f"[{now}] {method} {path}")
    g.logger.info(f"Headers: {headers}")
    if args:
        g.logger.info(f"Query Args: {args}")
    if body:
        try:
            body_str = body.decode('utf-8')
            if len(body_str) > 1000:  # 如果body太长，只记录前1000个字符
                body_str = body_str[:1000] + "..."
            g.logger.info(f"Request Body: {body_str}")
        except:
            g.logger.info("Request Body: <binary>")

@app.after_request
def log_response_info(response):
    # 记录响应状态码
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    g.logger.info(f"[{now}] Response Status: {response.status}")
    
    # 如果是JSON响应，记录响应内容
    if response.content_type == 'application/json':
        try:
            response_data = response.get_data().decode('utf-8')
            if len(response_data) > 1000:  # 如果响应太长，只记录前1000个字符
                response_data = response_data[:1000] + "..."
            g.logger.info(f"Response Data: {response_data}")
        except:
            g.logger.info("Response Data: <binary>")
    
    # 添加CORS头
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    
    return response

@app.route('/start', methods=['POST'])
def start_detect():
    """启动检测服务"""
    try:
        g.logger.info("收到启动检测请求")
        if not DEVICE_AUTHORIZED:
            g.logger.error("设备未授权，拒绝启动检测服务")
            return jsonify({
                'message': '设备未授权，无法启动检测服务',
                'code': 403
            }), 403

        g.START_FLAG = True
        # 检查当前状态，如果已经在运行中，直接返回成功
        if g.APP_STATUS == g.AppStatus.RUNNING:
            g.logger.info("检测服务已经在运行中，忽略重复启动请求")
            runtime_urls = _build_runtime_urls()
            return jsonify({
                'message': 'success',
                **runtime_urls,
                'code': 200
            })
        
        # 设置应用状态为运行中
        g.APP_STATUS = g.AppStatus.RUNNING
        g.logger.info(f"APP_STATUS: {g.APP_STATUS}")

        # 设置三色灯状态
        if n.TRICOLOUR_LIGHT_FLAG and g.TRICOLOUR_LIGHT_CLIENT is not None:
            running_status = n.TRICOLOUR_LIGHT_STATUS.get('running_status', {})
            g.TRICOLOUR_LIGHT_CLIENT.set_light(running_status.get('running', 'yellow'))
            g.logger.info("===light set running===")
        
        g.logger.info("检测服务已启动")
        runtime_urls = _build_runtime_urls()
        return jsonify({
            'message': 'success',
            **runtime_urls,
            'code': 200
        })
    except Exception as e:
        g.logger.error(f"启动检测服务失败: {e}")
        g.APP_STATUS = g.AppStatus.IDLE
        return jsonify({
            'message': f'启动检测服务失败: {str(e)}',
            'code': 500
        }), 500

@app.route('/stop', methods=['POST'])
def stop_detect():
    """停止检测服务"""
    try:
        g.logger.info("收到停止检测请求")
        
        # 检查当前状态，如果已经是空闲状态，直接返回成功
        if g.APP_STATUS == g.AppStatus.IDLE:
            g.logger.info("检测服务已经是空闲状态，忽略重复停止请求")
            return jsonify({
                'message': 'success',
                'code': 200
            })
        
        # 设置应用状态为空闲
        g.APP_STATUS = g.AppStatus.IDLE
        
        # 设置三色灯状态
        if n.TRICOLOUR_LIGHT_FLAG and g.TRICOLOUR_LIGHT_CLIENT is not None:
            running_status = n.TRICOLOUR_LIGHT_STATUS.get('running_status', {})
            g.TRICOLOUR_LIGHT_CLIENT.set_light(running_status.get('idle', 'yellow'))
            g.logger.info("===light set idle===")
        
        g.logger.info("检测服务已停止")
        return jsonify({
            'message': 'success',
            'code': 200
        })
        
    except Exception as e:
        g.logger.error(f"停止检测服务失败: {e}")
        return jsonify({
            'message': f'停止检测服务失败: {str(e)}',
            'code': 500
        }), 500

@app.route('/create_task', methods=['POST'])
def get_status():
    g.logger.info("收到获取状态请求")
    data = request.get_json()
    if n.ISP_CLOUD_CONFIG['create_task_path'] == "":
        throw_error("create_task_path is empty")
        return jsonify({
            'message': 'create_task_path is empty',
            'code': 400
        })
        
    request_data = {
        "frpc_port": int(g.FRP_PORT),
        "name": data['name'],
        "task_detail": data['task_detail']
    }
    try:
        response = casdoor_request.post(
            url=n.ISP_CLOUD_CONFIG['create_task_path'], 
            data=json.dumps(request_data),
        )
        if response.status_code != 200:
            throw_error(f"create task failed: {response.text}")
            return jsonify({
                'message': 'create task failed',
                'code': 400
            })
        return jsonify({
            'message': 'success',
            'code': 200
        })
    except Exception as e:
        throw_error(f"create task failed: {e}")

@app.route('/supported_boms', methods=['GET'])
def supported_boms():
    g.logger.info("收到获取支持的BOMs请求")
    if n.DEV_FLAG:
        return jsonify({    
            'code': 200,
            'data': {
                "bom_en": g.BOM_EN,
                "bom_zh": g.BOM_ZH
            }
        })
    else:
        return jsonify({
            'code': 200,
            'data': {
                "bom_en": ["luzhi"],
                "bom_zh": ["录制"]
            }
        })

@app.route('/pull_config', methods=['GET'])
def pull_config():
    g.logger.info("收到拉取配置请求")
    return jsonify({
        'code': 200,
        "data": {
            "DEV_FLAG": n.DEV_FLAG,
            "LIGHT_FLAG": n.TRICOLOUR_LIGHT_FLAG,
            "WORKSTATION_NAME": n.WORKSTATION_NAME
        }
    })

        
@app.route('/query_task', methods=['GET'])
def get_task_list():
    def _fallback_response(error_msg: str):
        try:
            local_data = read_tasks_content()
            if local_data:
                current_task_id = read_current_task_id()
                return jsonify({
                    'code': 200,
                    'data': local_data.get('data', []),
                    'task_id': current_task_id,
                    'message': '使用本地缓存数据'
                })
            g.logger.warning(f"{error_msg}, 且本地无缓存数据")
            return jsonify({
                'message': 'get task list failed and no local data available',
                'code': 400
            })
        except Exception as e:
            g.logger.error(f"读取本地任务数据失败: {e}")
            return jsonify({
                'message': 'failed to read local task data',
                'code': 500
            })

    if not n.DEV_FLAG:
        return jsonify({
            'code': 200,
            'data': [{
                "name": "录制任务",
                "task_id": "0",
                "detail": {
                    "luzhi": 1
                }
            }],
            'task_id': "0"
        })

    try:
        response = casdoor_request.post(
            url=n.ISP_CLOUD_CONFIG['task_list_query_path'],
            json_data={
                "frpc_port": int(g.FRP_PORT),
                "operating_station": n.WORKSTATION_NAME
            },
        )
    except Exception as e:
        g.logger.warning(f"get task list failed: {e}, 尝试从本地数据库读取")
        return _fallback_response("请求任务列表异常")

    try:
        data = response.json()
    except Exception as e:
        g.logger.error(f"解析响应JSON失败: {e}")
        return jsonify({
            'message': 'failed to parse response',
            'code': 500
        })

    if response.status_code != 200:
        g.logger.warning(f"get task list failed: {response.text}, 尝试从本地数据库读取")
        return _fallback_response("请求返回非 200")

    write_tasks_content(data)
    current_task_id = read_current_task_id()
    print("=========data=========")
    print(data)
    print("=========data=========")
    print(data.get('data', []))
    print("=========data end=========")

    # 注意：某些 Flask 版本/JSON provider 可能仍会对 jsonify 输出做 key 排序；
    # 为了保证 detail 顺序与上游/数据库插入顺序一致，这里直接用 json.dumps(sort_keys=False) 返回。
    payload = {
        'code': 200,
        'data': data.get('data', []),
        'task_id': current_task_id
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, sort_keys=False),
        mimetype='application/json'
    )

@app.route('/set_global_var', methods=['POST'])
def set_global_var():
    g.logger.info("收到设置全局变量请求")
    data = request.get_json()
    if not data:
        return jsonify({
            'message': '请求数据为空',
            'code': 400
        }), 400
        
    if data.get('name') == "LIGHT_FLAG":
        from src.hardware.tricolour_light import create_tricolour_light
        
        n.TRICOLOUR_LIGHT_FLAG = data.get('value', False)
        if n.TRICOLOUR_LIGHT_FLAG:
            # 如果还没有初始化，则初始化
            if g.TRICOLOUR_LIGHT_CLIENT is None:
                g.TRICOLOUR_LIGHT_CLIENT = create_tricolour_light()
            
            if g.TRICOLOUR_LIGHT_CLIENT is not None:
                running_status = n.TRICOLOUR_LIGHT_STATUS.get('running_status', {})
                if g.APP_STATUS == g.AppStatus.RUNNING:
                    g.TRICOLOUR_LIGHT_CLIENT.set_light(running_status.get('running', 'yellow'))
                    g.logger.info("===light set running===")
                else:
                    g.TRICOLOUR_LIGHT_CLIENT.set_light(running_status.get('idle', 'yellow'))
                    g.logger.info("===light set idle===")
        else:
            if g.TRICOLOUR_LIGHT_CLIENT is not None:
                g.TRICOLOUR_LIGHT_CLIENT.off_light()
                g.logger.info("===light set off===")
            g.TRICOLOUR_LIGHT_CLIENT = None

    file_path = './isp.json'
    # 加载JSON文件
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            json_data = json.load(file)
    except FileNotFoundError:
        g.logger.error(f"Error: File '{file_path}' not found.")
        return jsonify({
            'message': f"配置文件 '{file_path}' 不存在",
            'code': 500
        }), 500
    except json.JSONDecodeError:
        g.logger.error(f"Error: File '{file_path}' is not a valid JSON file.")
        return jsonify({
            'message': f"配置文件 '{file_path}' 格式错误",
            'code': 500
        }), 500
    
    # 将布尔值转换为字符串
    string_value = str(n.TRICOLOUR_LIGHT_FLAG)
    json_paths = ["TRICOLOUR_LIGHT_FLAG"]
    # 根据路径列表更新JSON数据
    current_level = json_data
    for key in json_paths[:-1]:  # 遍历到倒数第二个键
        if key in current_level:
            current_level = current_level[key]
        else:
            g.logger.error(f"Error: Key '{key}' not found in JSON data.")
            return jsonify({
                'message': f"配置键 '{key}' 不存在",
                'code': 500
            }), 500
    
    last_key = json_paths[-1]  # 最后一个键
    if last_key in current_level:
        current_level[last_key] = string_value
        g.logger.info(f"Updated '{' -> '.join(json_paths)}' to '{string_value}'")
    else:
        g.logger.error(f"Error: Key '{last_key}' not found in JSON data.")
        return jsonify({
            'message': f"配置键 '{last_key}' 不存在",
            'code': 500
        }), 500
    
    # 将更新后的JSON数据写回文件
    try:
        with open(file_path, 'w', encoding='utf-8') as file:
            json.dump(json_data, file, indent=4)
        g.logger.info("JSON file updated successfully.")
    except Exception as e:
        g.logger.error(f"写入配置文件失败: {e}")
        return jsonify({
            'message': f"写入配置文件失败: {str(e)}",
            'code': 500
        }), 500
    
    return jsonify({
        'message': 'success',
        'code': 200
    })
    
@app.route('/curr_task', methods=['POST'])
def set_curr_task():
    g.logger.info("收到获取任务详情请求")
    data = request.get_json()
    n.PROCESS_STATUS['current_task'] = data
    n.PROCESS_STATUS['current_status'] = read_count_by_task_id(n.PROCESS_STATUS['current_status'], n.PROCESS_STATUS['current_task']['task_id'])
    n.PROCESS_STATUS['current_task']['details'] = {}
    n.PROCESS_STATUS['current_status']['details'] = {}
    n.PROCESS_STATUS['current_status']['result'] = False
    for bom_name in n.PROCESS_STATUS['current_task']['detail']:
        n.PROCESS_STATUS['current_status']['details'][bom_name] = {
            "value": 0,
            "result": False
        }
    write_current_task_id(n.PROCESS_STATUS['current_task']['task_id'])
    n.ISP_CLOUD_CONFIG['cur_task_id'] = n.PROCESS_STATUS['current_task']['task_id']
    g.SOCKET_IO.emit('status', n.PROCESS_STATUS['current_status'])
    return jsonify({
        'code': 200
    })

# 为所有路由添加OPTIONS方法支持
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    return '', 200

@app.route('/check_ready', methods=['GET'])
def check_ready():
    return jsonify({
        'code': 200,
        'message': 'success'
    })

@app.route('/status', methods=['GET'])
def get_rdk_status():
    """获取 RDK 推理服务状态"""
    try:
        rdk_api_url = f"http://localhost:{g.FINISH_API_PORT}/status"
        response = requests.get(rdk_api_url, timeout=5)
        
        if response.status_code == 200:
            return jsonify(response.json()), 200
        else:
            return jsonify({
                'status': 'error',
                'message': f'RDK API error: {response.status_code}'
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'API error: {str(e)}'
        }), 500

@app.route('/device_status', methods=['GET'])
def get_device_status():
    """获取设备状态接口 - 用于前端轮询"""
    try:
        # 根据当前应用状态确定设备状态
        if g.APP_STATUS == g.AppStatus.RUNNING:
            status = "running"
        elif g.APP_STATUS == g.AppStatus.IDLE:
            status = "success"
        else:
            status = "ng"
        
        return jsonify({
            'device_id': 0,
            'status': status,
            'timestamp': datetime.datetime.now().isoformat(),
            **_build_runtime_urls(),
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'获取设备状态失败: {str(e)}'
        }), 500

@app.route('/restart_rdk', methods=['POST'])
def restart_rdk():
    """重启 RDK 推理服务 - 10秒内去重"""
    global last_restart_time
    
    current_time = time.time()
    
    # 检查是否在10秒内重复调用
    if current_time - last_restart_time < 10:
        g.logger.info("restart_rdk 接口在10秒内重复调用，返回成功")
        return jsonify({
            'code': 200,
            'message': 'success'
        })
    
    # 更新最后重启时间
    last_restart_time = current_time
    
    try:
        g.RDK_MANAGER.start_rdk()
        g.logger.info("RDK 推理服务重启成功")
        return jsonify({
            'code': 200,
            'message': 'success'
        })
    except Exception as e:
        g.logger.error(f"重启 RDK 推理服务失败: {e}")
        return jsonify({
            'code': 500,
            'message': f'重启 RDK 推理服务失败: {str(e)}'
        }), 500

def cleanup_resources():
    """清理资源"""
    try:
        if g.LABELS_SUBSCRIBER:
            g.LABELS_SUBSCRIBER.close()
            g.RDK_MANAGER.stop_rdk()
            g.logger.info("IPC订阅端已关闭")
    except Exception as e:
        g.logger.error(f"清理资源时出错: {e}")

if __name__ == '__main__':
    g.SOCKET_IO = SocketIO(
        app, 
        cors_allowed_origins="*",
        # 性能优化：增加心跳间隔，减少CPU消耗
        ping_interval=25,          # ping间隔25秒（默认值，但明确设置）
        ping_timeout=60,           # ping超时60秒
        async_mode='threading',    # 使用线程模式
        engineio_logger=False,     # 关闭engineio调试日志，减少IO开销
        logger=False,              # 关闭socketio调试日志，减少IO开销
    )

    def handle_socket_connect():
        sid = request.sid
        g.logger.info(f"Socket客户端连接成功: {sid}")
        _emit_cached_startup_check_failures_to_client(sid)

    g.SOCKET_IO.on_event('connect', handle_socket_connect)

    try:
        run_startup_self_checks()
    except Exception as e:
        message = f"启动自检执行异常: {e}"
        g.logger.error(message)
        _notify_startup_check_failure(
            _build_startup_check_payload(
                check_item="startup_self_check",
                message=message,
                level="error"
            )
        )
    
    # 注册退出处理
    import atexit
    atexit.register(cleanup_resources)
    
    def delayed_launch():
        import time
        time.sleep(2)  # 等待2秒确保Flask应用完全启动
        try:
            launch_thread()
            g.logger.info("工作线程启动完成")
        except Exception as e:
            g.logger.error(f"启动工作线程失败: {e}")

    def delayed_emit_start_event():
        if os.environ.get("ISP_RESTART_TRIGGER", "0") != "1":
            g.logger.info("本次不是 restart.sh 触发，跳过发送 start 刷新事件")
            return

        time.sleep(5)  # restart.sh 重启后等待5秒，再通知前端刷新
        try:
            g.SOCKET_IO.emit('start', {
                'reason': 'restart_script',
                'timestamp': datetime.datetime.now().isoformat()
            })
            g.logger.info("检测到 restart.sh 触发，已发送 start 刷新事件给前端")
        except Exception as e:
            g.logger.error(f"发送启动刷新事件失败: {e}")
    
    threading.Thread(target=delayed_launch, daemon=True).start()
    threading.Thread(target=delayed_emit_start_event, daemon=True).start()

    g.logger.info("启动Flask应用")
    try:
        g.SOCKET_IO.run(
            app,
            host='0.0.0.0', 
            port=g.ISP_PORT, 
            debug=False, 
            use_reloader=False,
            allow_unsafe_werkzeug=True, # 允许在生产环境中运行Werkzeug
        )
    except KeyboardInterrupt:
        g.logger.info("收到中断信号，正在关闭...")
        cleanup_resources()
    except Exception as e:
        g.logger.error(f"Flask应用启动失败: {e}")
        cleanup_resources()
        raise
