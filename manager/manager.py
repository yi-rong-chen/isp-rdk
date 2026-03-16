#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import argparse
import logging
import threading
import subprocess
import shutil
import re
import json
import ipaddress
import tempfile
from pathlib import Path
from typing import Optional, List, Dict

from flask import Flask, jsonify, request, send_file
from werkzeug.utils import secure_filename
from tricolour_light_manager import get_manager

APP_DIR = Path(__file__).resolve().parent
START_SCRIPT = APP_DIR / "start-application.sh"
STOP_SCRIPT  = APP_DIR / "stop-application.sh"
PROJECT_ROOT = APP_DIR.parent
PYC_BUILD_SCRIPT = PROJECT_ROOT / "build_edge_server_pyc.py"
PYC_COMPILE_TIMEOUT_SEC = 600
WORKSTATION_PYC_COMPILE_TIMEOUT_SEC = 180

# 工位相关配置
EDGE_SERVER_DIR = Path("/home/ccai/edge/edge-server")
UPLOAD_FOLDER = APP_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

DEFAULT_TIMEOUT_SEC = 120

# ==================== Netplan (网口配置) ====================
NETPLAN_DIR = Path("/etc/netplan")
# 可通过环境变量覆盖：NETPLAN_FILE=/etc/netplan/xxx.yaml
DEFAULT_NETPLAN_FILE = NETPLAN_DIR / "netplan.yaml"
# sudo 密码可通过环境变量覆盖：MANAGER_SUDO_PASSWORD=...
DEFAULT_SUDO_PASSWORD = "CCAIccai@1"

# ==================== FRP (frpc) 配置 ====================
FRP_INI_PATH = Path("/usr/local/frp/frpc.ini")

# 三色灯管理器
light_manager = None

# 防止 start/stop 同时跑（简单互斥）
LOCK = threading.Lock()
running_action: Optional[str] = None   # "start"/"stop"/None

def setup_logger(log_path=None, level=logging.INFO):
    logger = logging.getLogger("app_control")
    logger.setLevel(level)
    logger.propagate = False  # 防止传到 root logger 再打印一次

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_path:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger

# 方案A：这里只占位，不做真正初始化（避免重复添加 handler）
logger = logging.getLogger("app_control")


def _script_exists_and_exec(path: Path) -> Optional[str]:
    if not path.exists():
        return f"Script not found: {path}"
    if not os.access(path, os.X_OK):
        return f"Script not executable: {path} (chmod +x it)"
    return None

def compile_and_cleanup_sources(target_dir: Path, action_tag: str, timeout_sec: int = PYC_COMPILE_TIMEOUT_SEC) -> tuple[bool, str]:
    """调用统一脚本将目标目录下的 Python 源码编译为 pyc 并清理源码。"""
    if not PYC_BUILD_SCRIPT.exists():
        msg = f"pyc 构建脚本不存在: {PYC_BUILD_SCRIPT}"
        logger.error(f"[{action_tag}] {msg}")
        return False, msg

    if not target_dir.exists():
        msg = f"目标目录不存在: {target_dir}"
        logger.error(f"[{action_tag}] {msg}")
        return False, msg

    cmd = [sys.executable, str(PYC_BUILD_SCRIPT), str(target_dir)]
    logger.info(f"[{action_tag}] 执行 pyc 编译清理: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        msg = f"pyc 编译清理超时 (>{timeout_sec}s): {target_dir}"
        logger.error(f"[{action_tag}] {msg}")
        return False, msg
    except Exception as e:
        msg = f"调用 pyc 构建脚本异常: {e}"
        logger.exception(f"[{action_tag}] {msg}")
        return False, msg

    if result.stdout:
        logger.info(f"[{action_tag}] pyc 编译输出:\n{result.stdout}")
    if result.stderr:
        logger.warning(f"[{action_tag}] pyc 编译告警/错误:\n{result.stderr}")

    if result.returncode != 0:
        msg = f"pyc 编译清理失败，返回码: {result.returncode}"
        logger.error(f"[{action_tag}] {msg}")
        return False, msg

    logger.info(f"[{action_tag}] pyc 编译清理完成: {target_dir}")
    return True, "ok"

def run_script_bg(action: str, script_path: Path, timeout_sec: int):
    """后台线程执行脚本"""
    global running_action

    err = _script_exists_and_exec(script_path)
    if err:
        logger.error(f"[{action}] {err}")
        with LOCK:
            running_action = None
        return

    st = time.time()
    logger.info(f"[{action}] running {script_path.name} ...")

    try:
        if action == "start":
            ok_compile, compile_msg = compile_and_cleanup_sources(
                EDGE_SERVER_DIR,
                action_tag=f"{action}-precompile",
                timeout_sec=PYC_COMPILE_TIMEOUT_SEC,
            )
            if not ok_compile:
                logger.error(f"[{action}] precompile failed: {compile_msg}")
                with LOCK:
                    running_action = None
                return

        p = subprocess.Popen(
            ["/bin/bash", str(script_path)],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        try:
            out, err2 = p.communicate(timeout=timeout_sec)
            ok = (p.returncode == 0)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err2 = p.communicate()
            ok = False
            err2 = (err2 or "") + f"\nTimeout after {timeout_sec}s"

        ed = time.time()
        logger.info(f"[{action}] done ok={ok} exit={p.returncode} cost={ed-st:.3f}s")
        if out:
            logger.info(f"[{action}] stdout:\n{out}")
        if err2:
            logger.warning(f"[{action}] stderr:\n{err2}")

    except Exception as e:
        logger.exception(f"[{action}] exception: {e}")

    finally:
        with LOCK:
            running_action = None

def spawn_action(action: str, timeout_sec: int):
    """异步触发，立刻返回"""
    global running_action

    script = START_SCRIPT if action == "start" else STOP_SCRIPT

    with LOCK:
        if running_action is not None:
            # 简单起见：不阻塞，不重复执行，直接返回已在执行
            return False, f"another action is running: {running_action}"
        running_action = action

    th = threading.Thread(
        target=run_script_bg,
        args=(action, script, timeout_sec),
        daemon=True
    )
    th.start()
    return True, "accepted"

def scan_workstations() -> List[Dict]:
    """扫描所有工位目录"""
    workstations = []
    
    try:
        if not EDGE_SERVER_DIR.exists():
            logger.warning(f"Edge server directory not found: {EDGE_SERVER_DIR}")
            return workstations
        
        logger.debug(f"扫描工位目录: {EDGE_SERVER_DIR}")
        
        # 扫描数字目录
        for item in sorted(EDGE_SERVER_DIR.iterdir()):
            try:
                if item.is_dir() and item.name.isdigit():
                    ws_id = item.name
                    restart_script = item / "restart.sh"
                    config_file = item / "isp.json"
                    device_file = item / "device.json"
                    model_dir = item / "rdk"
                    model_file = None
                    if model_dir.exists():
                        for candidate_name in ("best.engine", "best.bin", "yolo11n_bayese_640x640_nv12.bin"):
                            candidate = model_dir / candidate_name
                            if candidate.exists():
                                model_file = candidate
                                break
                    
                    workstations.append({
                        "id": ws_id,
                        "path": str(item),
                        "has_restart_script": restart_script.exists(),
                        "has_config": config_file.exists(),
                        "has_device": device_file.exists(),
                        "is_activated": device_file.exists(),
                        "has_model": model_file.exists() if model_file else False,
                        "restart_script_path": str(restart_script),
                        "config_path": str(config_file),
                        "device_path": str(device_file),
                        "model_path": str(model_file) if model_file else ""
                    })
                    logger.debug(f"找到工位: {ws_id}, 路径: {item}, 激活状态: {device_file.exists()}")
            except Exception as e:
                logger.error(f"扫描工位目录 {item} 时出错: {e}", exc_info=True)
                continue
        
        logger.info(f"扫描完成，找到 {len(workstations)} 个工位")
        return workstations
    
    except Exception as e:
        logger.exception(f"扫描工位目录时发生异常: {e}")
        raise

def run_workstation_script(ws_id: str, script_name: str = "restart.sh") -> tuple:
    """执行工位的脚本"""
    ws_dir = EDGE_SERVER_DIR / ws_id
    script_path = ws_dir / script_name
    
    logger.info(f"[工位 {ws_id}] 准备执行脚本: {script_name}")
    logger.debug(f"[工位 {ws_id}] 工位目录: {ws_dir}")
    logger.debug(f"[工位 {ws_id}] 脚本路径: {script_path}")
    
    if not ws_dir.exists():
        error_msg = f"工位目录不存在: {ws_dir}"
        logger.error(f"[工位 {ws_id}] {error_msg}")
        return False, error_msg

    if script_name == "restart.sh":
        ok_compile, compile_msg = compile_and_cleanup_sources(
            ws_dir,
            action_tag=f"workstation-{ws_id}-precompile",
            timeout_sec=WORKSTATION_PYC_COMPILE_TIMEOUT_SEC,
        )
        if not ok_compile:
            error_msg = f"重启前 pyc 编译失败: {compile_msg}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return False, error_msg

    if not script_path.exists():
        error_msg = f"脚本不存在: {script_path}"
        logger.error(f"[工位 {ws_id}] {error_msg}")
        return False, error_msg
    
    if not os.access(script_path, os.X_OK):
        error_msg = f"脚本不可执行: {script_path} (需要 chmod +x {script_path})"
        logger.error(f"[工位 {ws_id}] {error_msg}")
        return False, error_msg
    
    try:
        logger.info(f"[工位 {ws_id}] 开始执行脚本: {script_path}")
        logger.debug(f"[工位 {ws_id}] 工作目录: {ws_dir}")
        
        result = subprocess.run(
            ["/bin/bash", str(script_path)],
            cwd=str(ws_dir),
            capture_output=True,
            text=True,
            timeout=60
        )
        
        logger.debug(f"[工位 {ws_id}] 脚本返回码: {result.returncode}")
        
        if result.stdout:
            logger.info(f"[工位 {ws_id}] 脚本输出 (stdout):\n{result.stdout}")
        
        if result.returncode == 0:
            logger.info(f"[工位 {ws_id}] 脚本执行成功")
            return True, "执行成功"
        else:
            error_msg = f"执行失败 (返回码: {result.returncode})"
            if result.stderr:
                error_msg += f"\n错误输出: {result.stderr}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            if result.stdout:
                logger.error(f"[工位 {ws_id}] 标准输出:\n{result.stdout}")
            return False, error_msg
    
    except subprocess.TimeoutExpired as e:
        error_msg = f"执行超时 (超过 60 秒)"
        logger.error(f"[工位 {ws_id}] {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f"执行脚本时发生异常: {str(e)}"
        logger.exception(f"[工位 {ws_id}] {error_msg}")
        return False, error_msg

def check_frpc_service() -> Dict:
    """检查 frpc 服务状态"""
    try:
        logger.debug("检查 frpc 服务状态")
        
        # 检查服务状态
        result = subprocess.run(
            ["systemctl", "status", "frpc.service"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        status_output = result.stdout + result.stderr
        
        # 解析服务状态
        is_active = "Active: active (running)" in status_output
        is_enabled = "enabled" in status_output
        
        # 检查是否包含 "start proxy success"
        has_proxy_success = "start proxy success" in status_output
        
        # 提取更多信息
        run_id_match = re.search(r'run id \[([^\]]+)\]', status_output)
        run_id = run_id_match.group(1) if run_id_match else None
        
        proxy_match = re.search(r'proxy added: \[([^\]]+)\]', status_output)
        proxy_name = proxy_match.group(1) if proxy_match else None
        
        logger.info(f"frpc 服务状态 - Active: {is_active}, Enabled: {is_enabled}, Proxy Success: {has_proxy_success}")
        
        return {
            "ok": True,
            "active": is_active,
            "enabled": is_enabled,
            "proxy_success": has_proxy_success,
            "run_id": run_id,
            "proxy_name": proxy_name,
            "status_output": status_output
        }
    
    except subprocess.TimeoutExpired:
        error_msg = "检查 frpc 服务超时"
        logger.error(error_msg)
        return {
            "ok": False,
            "active": False,
            "enabled": False,
            "proxy_success": False,
            "error": error_msg
        }
    except FileNotFoundError:
        error_msg = "systemctl 命令不可用（可能需要 root 权限）"
        logger.error(error_msg)
        return {
            "ok": False,
            "active": False,
            "enabled": False,
            "proxy_success": False,
            "error": error_msg
        }
    except Exception as e:
        error_msg = f"检查 frpc 服务时发生异常: {str(e)}"
        logger.exception(error_msg)
        return {
            "ok": False,
            "active": False,
            "enabled": False,
            "proxy_success": False,
            "error": error_msg
        }

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB 最大上传大小

@app.post("/start")
def start_api():
    timeout_sec = int(request.args.get("timeout", DEFAULT_TIMEOUT_SEC))
    ok, msg = spawn_action("start", timeout_sec)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

@app.post("/stop")
def stop_api():
    timeout_sec = int(request.args.get("timeout", DEFAULT_TIMEOUT_SEC))
    ok, msg = spawn_action("stop", timeout_sec)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

@app.get("/lights/status")
def lights_status_api():
    """获取所有三色灯的状态"""
    try:
        global light_manager
        if light_manager is None:
            light_manager = get_manager()
        
        status = light_manager.get_light_status()
        return jsonify({
            "ok": True,
            "lights": status,
            "count": len(status)
        }), 200
    except Exception as e:
        logger.error(f"获取三色灯状态失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/lights/initialize")
def lights_initialize_api():
    """初始化所有三色灯"""
    try:
        global light_manager
        if light_manager is None:
            light_manager = get_manager()
        
        force = request.args.get("force", "false").lower() == "true"
        auto_setup = request.args.get("auto_setup", "true").lower() == "true"
        success, results = light_manager.initialize_all(force=force, auto_setup=auto_setup)
        
        return jsonify({
            "ok": success,
            "results": results
        }), (200 if success else 500)
    except Exception as e:
        logger.error(f"初始化三色灯失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/lights/list")
def lights_list_api():
    """获取三色灯列表"""
    try:
        global light_manager
        if light_manager is None:
            light_manager = get_manager()
        
        light_list = light_manager.get_light_list()
        return jsonify({
            "ok": True,
            "lights": light_list,
            "count": len(light_list)
        }), 200
    except Exception as e:
        logger.error(f"获取三色灯列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/lights/test")
def lights_test_api():
    """
    测试三色灯
    参数:
        light_id: 灯的 ID，如 ttyUSB_light_0（必需）
    """
    try:
        global light_manager
        if light_manager is None:
            light_manager = get_manager()
        
        light_id = request.args.get("light_id")
        
        if not light_id:
            return jsonify({"ok": False, "error": "缺少参数 light_id"}), 400
        
        # 测试指定设备
        success, message = light_manager.test_light(light_id)
        return jsonify({
            "ok": success,
            "light_id": light_id,
            "message": message
        }), (200 if success else 500)
    except Exception as e:
        logger.error(f"测试三色灯失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/lights/close")
def lights_close_api():
    """关闭所有三色灯连接"""
    try:
        global light_manager
        if light_manager is not None:
            light_manager.close_all()
        
        return jsonify({"ok": True, "message": "所有三色灯已关闭"}), 200
    except Exception as e:
        logger.error(f"关闭三色灯失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/lights/create_symlinks")
def lights_create_symlinks_api():
    """手动创建符号链接（当 udev 规则未生效时的备用方案）"""
    try:
        global light_manager
        if light_manager is None:
            light_manager = get_manager()
        
        success, message = light_manager.create_symlinks_manually()
        return jsonify({
            "ok": success,
            "message": message
        }), (200 if success else 500)
    except Exception as e:
        logger.error(f"手动创建符号链接失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ==================== 工位管理 API ====================

@app.get("/workstations/list")
def workstations_list_api():
    """获取所有工位列表"""
    try:
        logger.info("收到获取工位列表请求")
        workstations = scan_workstations()
        logger.info(f"成功获取 {len(workstations)} 个工位")
        return jsonify({
            "ok": True,
            "workstations": workstations,
            "count": len(workstations)
        }), 200
    except Exception as e:
        error_msg = f"获取工位列表失败: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.post("/workstations/<ws_id>/restart")
def workstation_restart_api(ws_id: str):
    """重启指定工位"""
    try:
        logger.info(f"收到重启工位请求: {ws_id}")
        success, message = run_workstation_script(ws_id, "restart.sh")
        
        if success:
            logger.info(f"工位 {ws_id} 重启成功: {message}")
        else:
            logger.error(f"工位 {ws_id} 重启失败: {message}")
        
        return jsonify({
            "ok": success,
            "message": message
        }), (200 if success else 500)
    except Exception as e:
        error_msg = f"重启工位 {ws_id} 时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.post("/workstations/<ws_id>/stop")
def workstation_stop_api(ws_id: str):
    """停止指定工位服务"""
    try:
        logger.info(f"收到停止工位服务请求: {ws_id}")
        success, message = run_workstation_script(ws_id, "stop.sh")
        
        if success:
            logger.info(f"工位 {ws_id} 服务停止成功: {message}")
        else:
            logger.error(f"工位 {ws_id} 服务停止失败: {message}")
        
        return jsonify({
            "ok": success,
            "message": message
        }), (200 if success else 500)
    except Exception as e:
        error_msg = f"停止工位 {ws_id} 服务时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.get("/workstations/<ws_id>/get_config")
def workstation_get_config_api(ws_id: str):
    """获取工位配置文件"""
    try:
        logger.info(f"收到获取工位 {ws_id} 配置请求")
        
        ws_dir = EDGE_SERVER_DIR / ws_id
        config_path = ws_dir / "isp.json"
        
        logger.debug(f"[工位 {ws_id}] 工位目录: {ws_dir}")
        logger.debug(f"[工位 {ws_id}] 配置文件路径: {config_path}")
        
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        if not config_path.exists():
            error_msg = f"配置文件不存在: {config_path}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 读取配置文件
        logger.info(f"[工位 {ws_id}] 读取配置文件: {config_path}")
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        
        logger.info(f"[工位 {ws_id}] 配置文件读取成功，包含 {len(config_data)} 项配置")
        
        return jsonify({
            "ok": True,
            "config": config_data
        }), 200
    
    except json.JSONDecodeError as e:
        error_msg = f"配置文件格式错误: {str(e)}"
        logger.exception(f"[工位 {ws_id}] {error_msg}")
        return jsonify({"ok": False, "error": error_msg}), 500
    except Exception as e:
        error_msg = f"获取配置文件时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.post("/workstations/<ws_id>/save_config")
def workstation_save_config_api(ws_id: str):
    """保存工位配置文件"""
    try:
        logger.info(f"收到保存工位 {ws_id} 配置请求")
        
        # 获取 JSON 数据
        if not request.is_json:
            error_msg = "请求内容不是 JSON 格式"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        data = request.get_json()
        new_config = data.get("config", {})
        
        if not new_config:
            error_msg = "配置数据为空"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        logger.info(f"[工位 {ws_id}] 收到 {len(new_config)} 项配置")
        
        ws_dir = EDGE_SERVER_DIR / ws_id
        config_path = ws_dir / "isp.json"
        
        logger.debug(f"[工位 {ws_id}] 工位目录: {ws_dir}")
        logger.debug(f"[工位 {ws_id}] 配置文件路径: {config_path}")
        
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 创建备份目录
        backup_dir = ws_dir / "backup"
        backup_dir.mkdir(exist_ok=True)
        
        # 备份原配置文件到 backup 目录
        if config_path.exists():
            backup_filename = f"isp.json.backup.{int(time.time())}"
            backup_path = backup_dir / backup_filename
            logger.info(f"[工位 {ws_id}] 备份原配置文件: {config_path} -> {backup_path}")
            shutil.copy2(config_path, backup_path)
            logger.info(f"[工位 {ws_id}] 备份完成")
            
            # 删除原配置文件
            logger.info(f"[工位 {ws_id}] 删除原配置文件: {config_path}")
            config_path.unlink()
        else:
            logger.warning(f"[工位 {ws_id}] 原配置文件不存在，将创建新文件: {config_path}")
        
        # 保存新配置文件
        logger.info(f"[工位 {ws_id}] 保存新配置文件: {config_path}")
        logger.debug(f"[工位 {ws_id}] 配置内容: {list(new_config.keys())[:5]}... (共 {len(new_config)} 项)")
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, ensure_ascii=False, indent=2)
        
        # 验证文件已保存
        if config_path.exists():
            file_size = config_path.stat().st_size
            logger.info(f"[工位 {ws_id}] 配置文件已保存，文件大小: {file_size} 字节")
        else:
            logger.error(f"[工位 {ws_id}] 配置文件保存失败，文件不存在")
            return jsonify({
                "ok": False,
                "error": "配置文件保存失败"
            }), 500
        
        # 自动重启
        logger.info(f"[工位 {ws_id}] 开始自动重启...")
        success, message = run_workstation_script(ws_id, "restart.sh")
        
        if success:
            result_msg = f"配置已保存并重启成功: {message}"
            logger.info(f"[工位 {ws_id}] {result_msg}")
        else:
            result_msg = f"配置已保存，但重启失败: {message}"
            logger.error(f"[工位 {ws_id}] {result_msg}")
        
        return jsonify({
            "ok": success,
            "message": result_msg
        }), (200 if success else 500)
    
    except json.JSONDecodeError as e:
        error_msg = f"配置数据格式错误: {str(e)}"
        logger.exception(f"[工位 {ws_id}] {error_msg}")
        return jsonify({"ok": False, "error": error_msg}), 500
    except Exception as e:
        error_msg = f"保存配置文件时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.get("/workstations/<ws_id>/get_backups")
def workstation_get_backups_api(ws_id: str):
    """获取工位配置备份列表"""
    try:
        logger.info(f"收到获取工位 {ws_id} 备份列表请求")
        
        ws_dir = EDGE_SERVER_DIR / ws_id
        backup_dir = ws_dir / "backup"
        
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        if not backup_dir.exists():
            logger.info(f"[工位 {ws_id}] 备份目录不存在，返回空列表")
            return jsonify({
                "ok": True,
                "backups": [],
                "count": 0
            }), 200
        
        # 获取所有备份文件
        backup_files = []
        for item in backup_dir.iterdir():
            if item.is_file() and item.name.startswith("isp.json.backup."):
                try:
                    # 提取时间戳
                    timestamp_str = item.name.replace("isp.json.backup.", "")
                    timestamp = int(timestamp_str)
                    
                    # 获取文件信息
                    stat = item.stat()
                    backup_files.append({
                        "filename": item.name,
                        "timestamp": timestamp,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime
                    })
                except ValueError:
                    logger.warning(f"[工位 {ws_id}] 无效的备份文件名: {item.name}")
                    continue
        
        # 按时间戳降序排序（最新的在前）
        backup_files.sort(key=lambda x: x["timestamp"], reverse=True)
        
        logger.info(f"[工位 {ws_id}] 找到 {len(backup_files)} 个备份文件")
        
        return jsonify({
            "ok": True,
            "backups": backup_files,
            "count": len(backup_files)
        }), 200
    
    except Exception as e:
        error_msg = f"获取备份列表时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.post("/workstations/<ws_id>/rollback_config")
def workstation_rollback_config_api(ws_id: str):
    """回滚工位配置到最新备份"""
    try:
        logger.info(f"收到回滚工位 {ws_id} 配置请求")
        
        ws_dir = EDGE_SERVER_DIR / ws_id
        backup_dir = ws_dir / "backup"
        config_path = ws_dir / "isp.json"
        
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        if not backup_dir.exists() or not any(backup_dir.iterdir()):
            error_msg = "没有可用的备份文件"
            logger.warning(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 获取最新的备份文件
        backup_files = []
        for item in backup_dir.iterdir():
            if item.is_file() and item.name.startswith("isp.json.backup."):
                try:
                    timestamp_str = item.name.replace("isp.json.backup.", "")
                    timestamp = int(timestamp_str)
                    backup_files.append((timestamp, item))
                except ValueError:
                    continue
        
        if not backup_files:
            error_msg = "没有有效的备份文件"
            logger.warning(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 按时间戳排序，获取最新的备份
        backup_files.sort(reverse=True)
        latest_timestamp, latest_backup = backup_files[0]
        
        logger.info(f"[工位 {ws_id}] 找到最新备份: {latest_backup.name}")
        
        # 如果当前配置文件存在，先备份当前配置
        if config_path.exists():
            current_backup = backup_dir / f"isp.json.backup.{int(time.time())}"
            logger.info(f"[工位 {ws_id}] 备份当前配置: {config_path} -> {current_backup}")
            shutil.copy2(config_path, current_backup)
            
            # 删除当前配置
            logger.info(f"[工位 {ws_id}] 删除当前配置: {config_path}")
            config_path.unlink()
        
        # 恢复备份文件到配置路径
        logger.info(f"[工位 {ws_id}] 恢复备份: {latest_backup} -> {config_path}")
        shutil.copy2(latest_backup, config_path)
        
        # 删除已使用的备份文件
        logger.info(f"[工位 {ws_id}] 删除已使用的备份文件: {latest_backup}")
        latest_backup.unlink()
        
        # 验证恢复
        if config_path.exists():
            file_size = config_path.stat().st_size
            logger.info(f"[工位 {ws_id}] 配置已恢复，文件大小: {file_size} 字节")
        else:
            logger.error(f"[工位 {ws_id}] 配置恢复失败，文件不存在")
            return jsonify({
                "ok": False,
                "error": "配置恢复失败"
            }), 500
        
        # 自动重启
        logger.info(f"[工位 {ws_id}] 开始自动重启...")
        success, message = run_workstation_script(ws_id, "restart.sh")
        
        if success:
            result_msg = f"配置已回滚并重启成功"
            logger.info(f"[工位 {ws_id}] {result_msg}")
        else:
            result_msg = f"配置已回滚，但重启失败: {message}"
            logger.error(f"[工位 {ws_id}] {result_msg}")
        
        return jsonify({
            "ok": success,
            "message": result_msg,
            "backup_file": latest_backup.name
        }), (200 if success else 500)
    
    except Exception as e:
        error_msg = f"回滚配置时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.post("/workstations/<ws_id>/replace_config")
def workstation_replace_config_api(ws_id: str):
    """替换工位配置文件"""
    try:
        logger.info(f"收到替换工位 {ws_id} 配置文件请求")
        
        if 'file' not in request.files:
            error_msg = "未上传文件"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        file = request.files['file']
        logger.debug(f"[工位 {ws_id}] 上传文件名: {file.filename}")
        
        if file.filename == '':
            error_msg = "文件名为空"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        if not file.filename.endswith('.json'):
            error_msg = f"只支持 .json 文件，当前文件: {file.filename}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        ws_dir = EDGE_SERVER_DIR / ws_id
        logger.debug(f"[工位 {ws_id}] 工位目录: {ws_dir}")
        
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 保存上传的文件
        filename = secure_filename(file.filename)
        temp_path = UPLOAD_FOLDER / filename
        logger.info(f"[工位 {ws_id}] 保存上传文件到: {temp_path}")
        file.save(str(temp_path))
        logger.debug(f"[工位 {ws_id}] 文件大小: {temp_path.stat().st_size} 字节")
        
        # 备份原配置文件
        config_path = ws_dir / "isp.json"
        if config_path.exists():
            backup_path = ws_dir / f"isp.json.backup.{int(time.time())}"
            logger.info(f"[工位 {ws_id}] 备份原配置文件: {config_path} -> {backup_path}")
            shutil.copy2(config_path, backup_path)
            logger.info(f"[工位 {ws_id}] 备份完成")
        else:
            logger.warning(f"[工位 {ws_id}] 原配置文件不存在，将创建新文件: {config_path}")
        
        # 替换配置文件（重命名为 isp.json）
        logger.info(f"[工位 {ws_id}] 替换配置文件: {temp_path} -> {config_path}")
        shutil.copy2(temp_path, config_path)
        logger.info(f"[工位 {ws_id}] 配置文件已替换")
        
        # 删除临时文件
        temp_path.unlink()
        logger.debug(f"[工位 {ws_id}] 临时文件已删除")
        
        # 自动重启
        logger.info(f"[工位 {ws_id}] 开始自动重启...")
        success, message = run_workstation_script(ws_id, "restart.sh")
        
        if success:
            result_msg = f"配置文件已替换并重启成功: {message}"
            logger.info(f"[工位 {ws_id}] {result_msg}")
        else:
            result_msg = f"配置文件已替换，但重启失败: {message}"
            logger.error(f"[工位 {ws_id}] {result_msg}")
        
        return jsonify({
            "ok": success,
            "message": result_msg
        }), (200 if success else 500)
    
    except Exception as e:
        error_msg = f"替换工位 {ws_id} 配置文件时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.post("/workstations/<ws_id>/replace_model")
def workstation_replace_model_api(ws_id: str):
    """替换工位模型文件"""
    try:
        logger.info(f"收到替换工位 {ws_id} 模型文件请求")
        
        if 'file' not in request.files:
            error_msg = "未上传文件"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        file = request.files['file']
        logger.debug(f"[工位 {ws_id}] 上传文件名: {file.filename}")
        
        if file.filename == '':
            error_msg = "文件名为空"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        if not (file.filename.endswith('.engine') or file.filename.endswith('.bin')):
            error_msg = f"只支持 .engine 或 .bin 文件，当前文件: {file.filename}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        ws_dir = EDGE_SERVER_DIR / ws_id
        model_dir = ws_dir / "rdk"
        logger.debug(f"[工位 {ws_id}] 工位目录: {ws_dir}")
        logger.debug(f"[工位 {ws_id}] 模型目录: {model_dir}")
        
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        if not model_dir.exists():
            logger.info(f"[工位 {ws_id}] 模型目录不存在，创建: {model_dir}")
            model_dir.mkdir(parents=True)
            logger.info(f"[工位 {ws_id}] 模型目录创建完成")
        
        # 保存上传的文件
        filename = secure_filename(file.filename)
        temp_path = UPLOAD_FOLDER / filename
        logger.info(f"[工位 {ws_id}] 保存上传文件到: {temp_path}")
        file.save(str(temp_path))
        logger.debug(f"[工位 {ws_id}] 文件大小: {temp_path.stat().st_size} 字节")
        
        # 备份原模型文件
        target_name = "best.engine" if file.filename.endswith('.engine') else "best.bin"
        model_path = model_dir / target_name
        if model_path.exists():
            backup_path = model_dir / f"{target_name}.backup.{int(time.time())}"
            logger.info(f"[工位 {ws_id}] 备份原模型文件: {model_path} -> {backup_path}")
            shutil.copy2(model_path, backup_path)
            logger.info(f"[工位 {ws_id}] 备份完成")
        else:
            logger.warning(f"[工位 {ws_id}] 原模型文件不存在，将创建新文件: {model_path}")
        
        # 替换模型文件，保留当前后端对应的标准文件名
        logger.info(f"[工位 {ws_id}] 替换模型文件: {temp_path} -> {model_path}")
        shutil.copy2(temp_path, model_path)
        logger.info(f"[工位 {ws_id}] 模型文件已替换")
        
        # 删除临时文件
        temp_path.unlink()
        logger.debug(f"[工位 {ws_id}] 临时文件已删除")
        
        # 自动重启
        logger.info(f"[工位 {ws_id}] 开始自动重启...")
        success, message = run_workstation_script(ws_id, "restart.sh")
        
        if success:
            result_msg = f"模型文件已替换并重启成功: {message}"
            logger.info(f"[工位 {ws_id}] {result_msg}")
        else:
            result_msg = f"模型文件已替换，但重启失败: {message}"
            logger.error(f"[工位 {ws_id}] {result_msg}")
        
        return jsonify({
            "ok": success,
            "message": result_msg
        }), (200 if success else 500)
    
    except Exception as e:
        error_msg = f"替换工位 {ws_id} 模型文件时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

# ==================== 系统检查服务 API ====================

@app.get("/system/check_frpc")
def check_frpc_api():
    """检查 frpc 服务状态"""
    try:
        logger.info("收到检查 frpc 服务请求")
        result = check_frpc_service()
        return jsonify(result), 200
    except Exception as e:
        error_msg = f"检查 frpc 服务时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

# ==================== FRP (frpc) 配置 API ====================

def _find_netplan_file() -> Optional[Path]:
    """定位 netplan 配置文件"""
    env_path = os.environ.get("NETPLAN_FILE")
    if env_path:
        p = Path(env_path)
        return p if p.exists() else None

    if DEFAULT_NETPLAN_FILE.exists():
        return DEFAULT_NETPLAN_FILE

    if NETPLAN_DIR.exists():
        yamls = sorted(NETPLAN_DIR.glob("*.yaml")) + sorted(NETPLAN_DIR.glob("*.yml"))
        for p in yamls:
            if p.exists():
                return p
    return None

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def _atomic_write_text(path: Path, content: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

def _is_comment_or_blank(line: str) -> bool:
    s = line.strip()
    return (s == "") or s.startswith("#")

def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))

def _parse_netplan_interfaces(text: str) -> List[Dict]:
    """
    纯文本解析 netplan.yaml 中的 ethernets 配置：
    - 只解析 ethernets 下的一层网口名块
    - 解析 dhcp4 / addresses(第一条) / routes 中的 via(第一条)
    """
    lines = text.splitlines()
    eth_idx = None
    eth_indent = None

    for i, ln in enumerate(lines):
        if re.match(r"^\s*ethernets:\s*$", ln):
            eth_idx = i
            eth_indent = _indent_of(ln)
            break

    if eth_idx is None:
        return []

    interfaces: List[Dict] = []
    i = eth_idx + 1
    while i < len(lines):
        ln = lines[i]
        if _is_comment_or_blank(ln):
            i += 1
            continue

        ind = _indent_of(ln)
        # 退出 ethernets 块
        if ind <= eth_indent:
            break

        # 网口块：缩进比 ethernets 多 2，并且形如 "enP8p1s0:"
        m = re.match(r"^\s*([A-Za-z0-9_.:-]+)\s*:\s*$", ln)
        if m and ind == eth_indent + 2:
            iface = m.group(1)
            iface_indent = ind
            # 找块结束
            j = i + 1
            while j < len(lines):
                ln2 = lines[j]
                if _is_comment_or_blank(ln2):
                    j += 1
                    continue
                ind2 = _indent_of(ln2)
                if ind2 <= iface_indent:
                    break
                j += 1

            block = lines[i:j]
            dhcp4 = None
            addresses: List[str] = []
            address_first = ""   # 原始第一条（可能是 CIDR）
            ip_first = ""        # 第一条的 IP（忽略 /xx）
            gateway = ""         # 网关地址（routes 中的 via）

            # 解析 dhcp4
            for b in block[1:]:
                m_d = re.match(r"^\s*dhcp4:\s*(true|false)\s*$", b.strip())
                if m_d:
                    dhcp4 = (m_d.group(1) == "true")
                    break

            # 解析 addresses 第一条
            addr_line_idx = None
            for k in range(1, len(block)):
                if re.match(r"^\s*addresses:\s*$", block[k]):
                    addr_line_idx = k
                    break
            if addr_line_idx is not None:
                # list item 从下一行开始
                for k in range(addr_line_idx + 1, len(block)):
                    ln_a = block[k]
                    if _is_comment_or_blank(ln_a):
                        continue
                    m_item = re.match(r"^\s*-\s*(\S+)\s*$", ln_a)
                    if m_item:
                        addresses.append(m_item.group(1))
                    else:
                        # 遇到非 list item 停止
                        break
                if addresses:
                    address_first = addresses[0]
                    # 只取 IP 本体，不关心 /24 等前缀
                    ip_first = address_first.split("/", 1)[0]

            # 解析 routes 中的 via（第一条默认路由的网关）
            routes_line_idx = None
            for k in range(1, len(block)):
                if re.match(r"^\s*routes:\s*$", block[k]):
                    routes_line_idx = k
                    break
            if routes_line_idx is not None:
                # routes 是列表，找第一个 route 块
                route_indent = None
                for k in range(routes_line_idx + 1, len(block)):
                    ln_r = block[k]
                    if _is_comment_or_blank(ln_r):
                        continue
                    # 第一个 list item: "- to: ..."
                    m_route_item = re.match(r"^\s*-\s+", ln_r)
                    if m_route_item:
                        route_indent = _indent_of(ln_r)
                        # 解析这个route块内的via
                        # 可能是 "- to: xxx" 后面跟 "  via: xxx"
                        # 或者是 "- {to: xxx, via: xxx}"
                        # 先尝试解析同一行
                        via_match = re.search(r"via:\s*(\S+)", ln_r)
                        if via_match:
                            gateway = via_match.group(1)
                            break
                        # 否则看下几行
                        for kk in range(k + 1, len(block)):
                            ln_via = block[kk]
                            if _is_comment_or_blank(ln_via):
                                continue
                            # 如果缩进回到同级或更少，说明route块结束
                            if _indent_of(ln_via) <= route_indent:
                                break
                            via_match2 = re.match(r"^\s*via:\s*(\S+)", ln_via)
                            if via_match2:
                                gateway = via_match2.group(1)
                                break
                        break

            interfaces.append({
                "name": iface,
                "dhcp4": dhcp4,
                "addresses": addresses,
                # 为兼容 UI：address 字段返回 IP（不含 /xx）
                "address": ip_first,
                # 额外字段：原始 CIDR（如需要排查/调试）
                "cidr": address_first,
                "ip": ip_first,
                "gateway": gateway,
            })

            i = j
            continue

        i += 1

    return interfaces

def _set_interface_ip_in_text(text: str, iface: str, new_ip: str, default_prefix: str = "24") -> str:
    """
    在 netplan 文本中，替换指定 iface 的 addresses 第一条里的 IP（斜杠前部分）。
    - 保留原来的前缀长度（/xx）
    - 如果原来没有 /xx，则使用 default_prefix（默认 24）
    """
    try:
        ipaddress.ip_address(new_ip)
    except Exception:
        raise ValueError("ip 必须是合法 IPv4/IPv6 地址，例如 192.169.1.10")

    lines = text.splitlines(True)  # keepends
    # 找 ethernets
    eth_idx = None
    eth_indent = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*ethernets:\s*$", ln):
            eth_idx = i
            eth_indent = _indent_of(ln)
            break
    if eth_idx is None:
        raise ValueError("netplan 文件中未找到 ethernets 配置块")

    # 找 iface 行
    iface_idx = None
    iface_indent = None
    iface_pat = re.compile(rf"^\s*{re.escape(iface)}\s*:\s*$")
    for i in range(eth_idx + 1, len(lines)):
        ln = lines[i]
        if _is_comment_or_blank(ln):
            continue
        ind = _indent_of(ln)
        if ind <= eth_indent:
            break
        if iface_pat.match(ln) and ind == eth_indent + 2:
            iface_idx = i
            iface_indent = ind
            break

    if iface_idx is None or iface_indent is None:
        raise ValueError(f"未找到网口: {iface}")

    # iface 块结束
    end = iface_idx + 1
    while end < len(lines):
        ln = lines[end]
        if _is_comment_or_blank(ln):
            end += 1
            continue
        if _indent_of(ln) <= iface_indent:
            break
        end += 1

    # 在 iface 块内找 addresses:
    addr_key_idx = None
    for i in range(iface_idx + 1, end):
        if re.match(r"^\s*addresses:\s*$", lines[i]):
            addr_key_idx = i
            break

    addresses_indent = iface_indent + 2
    item_indent = iface_indent + 4
    addr_key_line = (" " * addresses_indent) + "addresses:\n"

    def _compose_value(existing: str) -> str:
        # existing 可能是 "192.169.1.10/24" 或 "192.169.1.10"
        if "/" in existing:
            _, prefix = existing.split("/", 1)
            prefix = prefix.strip() or default_prefix
        else:
            prefix = default_prefix
        return f"{new_ip}/{prefix}"

    if addr_key_idx is not None:
        # 找第一条 list item
        first_item_idx = None
        existing_value = ""
        i = addr_key_idx + 1
        while i < end:
            ln = lines[i]
            if _is_comment_or_blank(ln):
                i += 1
                continue
            m_item = re.match(r"^\s*-\s*(\S+)\s*$", ln)
            if m_item:
                first_item_idx = i
                existing_value = m_item.group(1)
            break

        if first_item_idx is not None:
            new_value = _compose_value(existing_value)
            lines[first_item_idx] = (" " * _indent_of(lines[first_item_idx])) + f"- {new_value}\n"
        else:
            # addresses 有但没有 list item，就插入一条（默认 /24）
            new_value = f"{new_ip}/{default_prefix}"
            item_line = (" " * item_indent) + f"- {new_value}\n"
            lines.insert(addr_key_idx + 1, item_line)
        return "".join(lines)

    # addresses 不存在：插入到 dhcp4 之后（如果有），否则插入到 iface 行之后
    insert_at = iface_idx + 1
    for i in range(iface_idx + 1, end):
        if re.match(r"^\s*dhcp4:\s*(true|false)\s*$", lines[i].strip()):
            insert_at = i + 1
            break

    item_line = (" " * item_indent) + f"- {new_ip}/{default_prefix}\n"
    lines.insert(insert_at, addr_key_line)
    lines.insert(insert_at + 1, item_line)
    return "".join(lines)

def _set_interface_ip_gateway_in_text(text: str, iface: str, new_ip: str, new_gateway: str = None, default_prefix: str = "24") -> str:
    """
    在 netplan 文本中，同时设置指定 iface 的 IP 和网关（gateway/via）。
    - 先设置 IP（addresses 第一条）
    - 再设置网关（routes 第一条的 via）
    """
    # 验证 IP
    try:
        ipaddress.ip_address(new_ip)
    except Exception:
        raise ValueError("ip 必须是合法 IPv4/IPv6 地址，例如 192.169.1.10")
    
    # 验证网关（如果提供）
    if new_gateway:
        try:
            ipaddress.ip_address(new_gateway)
        except Exception:
            raise ValueError("gateway 必须是合法 IPv4/IPv6 地址，例如 192.169.1.1")

    lines = text.splitlines(True)  # keepends
    
    # 找 ethernets
    eth_idx = None
    eth_indent = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*ethernets:\s*$", ln):
            eth_idx = i
            eth_indent = _indent_of(ln)
            break
    if eth_idx is None:
        raise ValueError("netplan 文件中未找到 ethernets 配置块")

    # 找 iface 行
    iface_idx = None
    iface_indent = None
    iface_pat = re.compile(rf"^\s*{re.escape(iface)}\s*:\s*$")
    for i in range(eth_idx + 1, len(lines)):
        ln = lines[i]
        if _is_comment_or_blank(ln):
            continue
        ind = _indent_of(ln)
        if ind <= eth_indent:
            break
        if iface_pat.match(ln) and ind == eth_indent + 2:
            iface_idx = i
            iface_indent = ind
            break

    if iface_idx is None or iface_indent is None:
        raise ValueError(f"未找到网口: {iface}")

    # iface 块结束
    end = iface_idx + 1
    while end < len(lines):
        ln = lines[end]
        if _is_comment_or_blank(ln):
            end += 1
            continue
        if _indent_of(ln) <= iface_indent:
            break
        end += 1

    # === 第一步：设置 IP (addresses) ===
    addr_key_idx = None
    for i in range(iface_idx + 1, end):
        if re.match(r"^\s*addresses:\s*$", lines[i]):
            addr_key_idx = i
            break

    addresses_indent = iface_indent + 2
    item_indent = iface_indent + 4

    def _compose_value(existing: str) -> str:
        if "/" in existing:
            _, prefix = existing.split("/", 1)
            prefix = prefix.strip() or default_prefix
        else:
            prefix = default_prefix
        return f"{new_ip}/{prefix}"

    if addr_key_idx is not None:
        # 找第一条 list item
        first_item_idx = None
        existing_value = ""
        i = addr_key_idx + 1
        while i < end:
            ln = lines[i]
            if _is_comment_or_blank(ln):
                i += 1
                continue
            m_item = re.match(r"^\s*-\s*(\S+)\s*$", ln)
            if m_item:
                first_item_idx = i
                existing_value = m_item.group(1)
            break

        if first_item_idx is not None:
            new_value = _compose_value(existing_value)
            lines[first_item_idx] = (" " * _indent_of(lines[first_item_idx])) + f"- {new_value}\n"
        else:
            new_value = f"{new_ip}/{default_prefix}"
            item_line = (" " * item_indent) + f"- {new_value}\n"
            lines.insert(addr_key_idx + 1, item_line)
    else:
        # addresses 不存在：插入到 dhcp4 之后
        insert_at = iface_idx + 1
        for i in range(iface_idx + 1, end):
            if re.match(r"^\s*dhcp4:\s*(true|false)\s*$", lines[i].strip()):
                insert_at = i + 1
                break
        addr_key_line = (" " * addresses_indent) + "addresses:\n"
        item_line = (" " * item_indent) + f"- {new_ip}/{default_prefix}\n"
        lines.insert(insert_at, addr_key_line)
        lines.insert(insert_at + 1, item_line)
        # 更新 end（插入了2行）
        end += 2

    # === 第二步：设置网关 (routes 中的 via) ===
    if new_gateway:
        # 重新计算 end（因为前面可能插入了行）
        end = iface_idx + 1
        while end < len(lines):
            ln = lines[end]
            if _is_comment_or_blank(ln):
                end += 1
                continue
            if _indent_of(ln) <= iface_indent:
                break
            end += 1

        # 找 routes:
        routes_key_idx = None
        for i in range(iface_idx + 1, end):
            if re.match(r"^\s*routes:\s*$", lines[i]):
                routes_key_idx = i
                break

        routes_indent = iface_indent + 2
        route_item_indent = iface_indent + 4
        route_field_indent = iface_indent + 6

        if routes_key_idx is not None:
            # 找第一个 route item（"- to: ..."）
            first_route_idx = None
            via_line_idx = None
            i = routes_key_idx + 1
            while i < end:
                ln = lines[i]
                if _is_comment_or_blank(ln):
                    i += 1
                    continue
                # 检查是否是 list item
                m_route = re.match(r"^\s*-\s+", ln)
                if m_route:
                    first_route_idx = i
                    route_start_indent = _indent_of(ln)
                    # 在这个 route 块内找 via:
                    j = i
                    while j < end:
                        ln2 = lines[j]
                        if _is_comment_or_blank(ln2):
                            j += 1
                            continue
                        # 如果缩进回到同级或更少，说明route块结束
                        if j > i and _indent_of(ln2) <= route_start_indent:
                            break
                        # 查找 via: 行
                        if re.match(r"^\s*via:\s*", ln2):
                            via_line_idx = j
                            break
                        j += 1
                    break
                i += 1

            if via_line_idx is not None:
                # 替换现有的 via 行
                indent = _indent_of(lines[via_line_idx])
                lines[via_line_idx] = (" " * indent) + f"via: {new_gateway}\n"
            elif first_route_idx is not None:
                # via 不存在，但 route 存在，在 route 块内插入 via
                # 找到这个 route item 的最后一行
                j = first_route_idx + 1
                route_start_indent = _indent_of(lines[first_route_idx])
                while j < end:
                    ln2 = lines[j]
                    if _is_comment_or_blank(ln2):
                        j += 1
                        continue
                    if _indent_of(ln2) <= route_start_indent:
                        break
                    j += 1
                # 在 j 位置插入 via
                via_line = (" " * route_field_indent) + f"via: {new_gateway}\n"
                lines.insert(j, via_line)
            else:
                # routes 存在但没有 route item，插入一个完整的 route
                route_lines = [
                    (" " * route_item_indent) + "- to: 0.0.0.0/0\n",
                    (" " * route_field_indent) + f"via: {new_gateway}\n",
                    (" " * route_field_indent) + "metric: 800\n",
                ]
                for idx, route_line in enumerate(route_lines):
                    lines.insert(routes_key_idx + 1 + idx, route_line)
        else:
            # routes 不存在，插入完整的 routes 块
            # 插入位置：在 addresses 之后，或 optional 之前，或 nameservers 之前
            insert_at = end
            for i in range(iface_idx + 1, end):
                if re.match(r"^\s*(optional|nameservers):\s*", lines[i]):
                    insert_at = i
                    break
            
            routes_lines = [
                (" " * routes_indent) + "routes:\n",
                (" " * route_item_indent) + "- to: 0.0.0.0/0\n",
                (" " * route_field_indent) + f"via: {new_gateway}\n",
                (" " * route_field_indent) + "metric: 800\n",
            ]
            for idx, route_line in enumerate(routes_lines):
                lines.insert(insert_at + idx, route_line)

    return "".join(lines)

def _sudo_netplan_apply(timeout_sec: int = 30) -> Dict:
    """执行 sudo netplan apply（通过 -S 从 stdin 读密码）。"""
    sudo_pass = os.environ.get("MANAGER_SUDO_PASSWORD", DEFAULT_SUDO_PASSWORD)
    try:
        result = subprocess.run(
            ["sudo", "-S", "netplan", "apply"],
            input=sudo_pass + "\n",
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        ok = (result.returncode == 0)
        return {
            "ok": ok,
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }
    except FileNotFoundError as e:
        return {"ok": False, "error": f"命令不存在: {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"netplan apply 超时（>{timeout_sec}s）"}
    except Exception as e:
        return {"ok": False, "error": f"执行 netplan apply 异常: {e}"}

def _sudo_systemctl(args: List[str], timeout_sec: int = 30) -> Dict:
    """执行 sudo systemctl ...（通过 -S 从 stdin 读密码）。"""
    sudo_pass = os.environ.get("MANAGER_SUDO_PASSWORD", DEFAULT_SUDO_PASSWORD)
    try:
        result = subprocess.run(
            ["sudo", "-S", "systemctl", *args],
            input=sudo_pass + "\n",
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        ok = (result.returncode == 0)
        return {
            "ok": ok,
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }
    except FileNotFoundError as e:
        return {"ok": False, "error": f"命令不存在: {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"systemctl 超时（>{timeout_sec}s）"}
    except Exception as e:
        return {"ok": False, "error": f"执行 systemctl 异常: {e}"}

def _sudo_write_text(path: Path, content: str, timeout_sec: int = 30) -> Dict:
    """
    使用 sudo 覆盖写入文件（不生成备份）。
    为避免 sudo 时间戳缓存导致"密码行被写入文件"，这里不使用 `sudo tee` 直接写入；
    改为：先写到用户可写临时文件，再 `sudo install` 覆盖到目标路径。
    """
    sudo_pass = os.environ.get("MANAGER_SUDO_PASSWORD", DEFAULT_SUDO_PASSWORD)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        # install 在 root 权限下落盘，不读取 stdin（stdin 只用于 sudo 读密码；即使 sudo 不读，也不会写进目标文件）
        result = subprocess.run(
            ["sudo", "-S", "install", "-o", "root", "-g", "root", "-m", "644", tmp_path, str(path)],
            input=sudo_pass + "\n",
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        ok = (result.returncode == 0)
        return {
            "ok": ok,
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }
    except FileNotFoundError as e:
        return {"ok": False, "error": f"命令不存在: {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"sudo 写入文件超时（>{timeout_sec}s）"}
    except Exception as e:
        return {"ok": False, "error": f"sudo 写入文件异常: {e}"}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

def _sudo_read_text(path: Path, timeout_sec: int = 10) -> Dict:
    """当普通读失败时，用 sudo 读取文件内容。"""
    sudo_pass = os.environ.get("MANAGER_SUDO_PASSWORD", DEFAULT_SUDO_PASSWORD)
    try:
        result = subprocess.run(
            ["sudo", "-S", "cat", str(path)],
            input=sudo_pass + "\n",
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        ok = (result.returncode == 0)
        if ok:
            return {"ok": True, "content": result.stdout or ""}
        return {"ok": False, "returncode": result.returncode, "stdout": result.stdout or "", "stderr": result.stderr or ""}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"命令不存在: {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"sudo 读取文件超时（>{timeout_sec}s）"}
    except Exception as e:
        return {"ok": False, "error": f"sudo 读取文件异常: {e}"}

def _read_frp_ini_text() -> Dict:
    """读取 frp.ini（优先普通读取，失败则 sudo cat）。"""
    if not FRP_INI_PATH.exists():
        return {"ok": False, "error": f"frpc.ini 不存在: {FRP_INI_PATH}"}
    try:
        return {"ok": True, "content": FRP_INI_PATH.read_text(encoding="utf-8")}
    except Exception as e:
        # 权限不足时尝试 sudo
        sudo_r = _sudo_read_text(FRP_INI_PATH)
        if sudo_r.get("ok"):
            return {"ok": True, "content": sudo_r.get("content", "")}
        return {"ok": False, "error": f"读取 frpc.ini 失败: {e}", "sudo": sudo_r}

def _parse_frp_ini_status(text: str) -> Dict:
    """
    判断是否为模板：
      - 存在 [ssh_] 段
      - 且该段 remote_port 为空（remote_port = 或 remote_port =     ）
    已初始化：
      - [ssh_6305] 这种段名，且 remote_port 为数字
    """
    lines = text.splitlines()
    ssh_section = None
    ssh_port = None
    remote_port = None
    in_ssh = False

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m_sec = re.match(r"^\[([^\]]+)\]$", line)
        if m_sec:
            sec = m_sec.group(1).strip()
            in_ssh = sec.startswith("ssh_")
            if in_ssh:
                ssh_section = sec
                m_port = re.match(r"^ssh_(\d+)$", sec)
                if m_port:
                    ssh_port = m_port.group(1)
            continue

        if in_ssh:
            m_rp = re.match(r"^remote_port\s*=\s*(.*)$", line)
            if m_rp:
                remote_port = m_rp.group(1).strip()

    needs_init = (ssh_section == "ssh_" and (remote_port is None or remote_port == ""))
    initialized = (ssh_section is not None and ssh_section != "ssh_" and (remote_port or "").isdigit())

    current_port = None
    if initialized and (remote_port or "").isdigit():
        current_port = int(remote_port)
    elif (ssh_port or "").isdigit():
        current_port = int(ssh_port)

    return {
        "ssh_section": ssh_section,
        "remote_port": remote_port,
        "needs_init": needs_init,
        "initialized": initialized,
        "current_port": current_port,
    }

def _frp_ini_set_ssh_port(text: str, port: int) -> str:
    """把模板里的 [ssh_] + remote_port 空值改成 [ssh_<port>] + remote_port=<port>。"""
    if port <= 0 or port > 65535:
        raise ValueError("port 必须在 1-65535")

    lines = text.splitlines(True)  # keepends
    out: List[str] = []
    in_ssh = False
    saw_ssh_section = False
    replaced_remote = False

    for ln in lines:
        stripped = ln.strip()
        m_sec = re.match(r"^\[([^\]]+)\]\s*$", stripped)
        if m_sec:
            sec = m_sec.group(1).strip()
            if sec == "ssh_":
                out.append(f"[ssh_{port}]\n")
                in_ssh = True
                saw_ssh_section = True
                continue
            # 进入其他 section：如果刚离开 ssh_ 段但没找到 remote_port，就补一行
            if in_ssh and saw_ssh_section and (not replaced_remote):
                out.append(f"remote_port = {port}\n")
                replaced_remote = True
            in_ssh = False
            out.append(ln)
            continue

        if in_ssh:
            m_rp = re.match(r"^\s*remote_port\s*=\s*(.*)\s*$", ln)
            if m_rp:
                # 保留原缩进
                indent = re.match(r"^(\s*)", ln).group(1)
                out.append(f"{indent}remote_port = {port}\n")
                replaced_remote = True
                continue

        out.append(ln)

    # 文件结束时，仍在 ssh_ 段且没写 remote_port，则追加
    if in_ssh and saw_ssh_section and (not replaced_remote):
        out.append(f"remote_port = {port}\n")

    if not saw_ssh_section:
        raise ValueError("frpc.ini 未找到模板段 [ssh_]")

    return "".join(out)

@app.get("/frp/status")
def frp_status_api():
    """读取 /usr/local/frp/frp.ini，判断是否为模板、当前端口等。"""
    try:
        r = _read_frp_ini_text()
        if not r.get("ok"):
            return jsonify({"ok": False, "error": r.get("error", "读取失败"), "detail": r}), 404
        text = r.get("content", "")
        st = _parse_frp_ini_status(text)
        return jsonify({
            "ok": True,
            "path": str(FRP_INI_PATH),
            **st,
        }), 200
    except Exception as e:
        logger.exception(f"读取 FRP 状态失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/frp/initialize")
def frp_initialize_api():
    """
    仅初始化一次：
      - 当 frpc.ini 为模板（[ssh_] 且 remote_port 为空）时允许初始化
      - 初始化完成后不允许再次修改（返回 409）
    参数（query 或 JSON）:
      - port: 例如 6305
    """
    try:
        payload = request.get_json(silent=True) or {}
        raw_port = payload.get("port") or request.args.get("port")
        if raw_port is None or str(raw_port).strip() == "":
            return jsonify({"ok": False, "error": "缺少参数 port（例如 6305）"}), 400

        if not str(raw_port).strip().isdigit():
            return jsonify({"ok": False, "error": "port 必须是数字"}), 400

        port = int(str(raw_port).strip())
        if port <= 0 or port > 65535:
            return jsonify({"ok": False, "error": "port 范围必须是 1-65535"}), 400

        r = _read_frp_ini_text()
        if not r.get("ok"):
            return jsonify({"ok": False, "error": r.get("error", "读取失败"), "detail": r}), 404

        text = r.get("content", "")
        st = _parse_frp_ini_status(text)
        if not st.get("needs_init"):
            # 已初始化或不是模板：禁止修改
            return jsonify({
                "ok": False,
                "error": "frpc.ini 已初始化，禁止修改",
                "path": str(FRP_INI_PATH),
                **st,
            }), 409

        new_text = _frp_ini_set_ssh_port(text, port)

        write_result = _sudo_write_text(FRP_INI_PATH, new_text)
        if not write_result.get("ok"):
            err = write_result.get("error") or write_result.get("stderr") or "写入 frpc.ini 失败"
            return jsonify({
                "ok": False,
                "error": err,
                "path": str(FRP_INI_PATH),
                "write": write_result,
            }), 500

        enable_result = _sudo_systemctl(["enable", "frpc.service"])
        restart_result = _sudo_systemctl(["restart", "frpc.service"])

        ok = enable_result.get("ok") and restart_result.get("ok")
        http_code = 200 if ok else 500

        # 返回最新状态（尽量）
        st2 = _parse_frp_ini_status(new_text)
        return jsonify({
            "ok": ok,
            "path": str(FRP_INI_PATH),
            "port": port,
            "write": write_result,
            "systemctl_enable": enable_result,
            "systemctl_restart": restart_result,
            **st2,
        }), http_code

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logger.exception(f"初始化 FRP 失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ==================== 网口设置 API（netplan） ====================

@app.get("/netplan/interfaces")
def netplan_interfaces_api():
    """读取 netplan 配置，返回 ethernets 下的网口与 addresses"""
    try:
        path = _find_netplan_file()
        if not path:
            return jsonify({"ok": False, "error": "未找到 netplan 配置文件（默认 /etc/netplan/netplan.yaml）"}), 404
        text = _read_text(path)
        interfaces = _parse_netplan_interfaces(text)
        return jsonify({
            "ok": True,
            "netplan_file": str(path),
            "interfaces": interfaces,
            "count": len(interfaces),
        }), 200
    except Exception as e:
        logger.exception(f"读取 netplan 失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/netplan/interfaces/<iface>/address")
def netplan_set_interface_address_api(iface: str):
    """
    设置指定网口的 IP 和网关（gateway），并默认执行 sudo netplan apply。
    参数（query 或 JSON 都支持）:
      - ip: IP 字符串，例如 192.169.1.10（必填）
      - address: 兼容旧参数；若传入 CIDR，将自动忽略 /xx，仅取 IP 部分
      - gateway: 网关地址，例如 192.169.1.1（可选）
      - apply: "true"/"false"（可选，默认 true）
    """
    try:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("ip") or payload.get("address") or request.args.get("ip") or request.args.get("address")
        gateway = payload.get("gateway") or request.args.get("gateway")
        apply_flag = (payload.get("apply") if "apply" in payload else request.args.get("apply", "true"))
        apply_flag = str(apply_flag).lower() == "true"

        if not raw:
            return jsonify({"ok": False, "error": "缺少参数 ip（例如 192.169.1.10）"}), 400

        # 兼容：如果传入的是 CIDR，只取 IP 本体
        ip_value = str(raw).strip().split("/", 1)[0]
        
        # 处理网关（如果提供）
        gateway_value = None
        if gateway:
            gateway_value = str(gateway).strip()

        path = _find_netplan_file()
        if not path:
            return jsonify({"ok": False, "error": "未找到 netplan 配置文件"}), 404

        old_text = _read_text(path)
        
        # 使用新的函数同时设置 IP 和网关
        if gateway_value:
            new_text = _set_interface_ip_gateway_in_text(old_text, iface, ip_value, gateway_value)
        else:
            # 如果没有提供网关，仅设置 IP
            new_text = _set_interface_ip_in_text(old_text, iface, ip_value)
            
        if new_text == old_text:
            # 仍然可以选择 apply
            apply_result = _sudo_netplan_apply() if apply_flag else {"ok": True, "skipped": True}
            return jsonify({
                "ok": True,
                "netplan_file": str(path),
                "interface": iface,
                "ip": ip_value,
                "gateway": gateway_value,
                "changed": False,
                "apply": apply_result,
            }), 200

        # 直接 sudo 覆盖写入（不生成备份）
        write_result = _sudo_write_text(path, new_text)
        if not write_result.get("ok"):
            err = write_result.get("error") or write_result.get("stderr") or "写入 netplan 失败"
            return jsonify({
                "ok": False,
                "netplan_file": str(path),
                "interface": iface,
                "ip": ip_value,
                "gateway": gateway_value,
                "changed": False,
                "write": write_result,
                "error": err,
            }), 500

        apply_result = _sudo_netplan_apply() if apply_flag else {"ok": True, "skipped": True}
        http_code = 200 if apply_result.get("ok") else 500
        return jsonify({
            "ok": apply_result.get("ok", False),
            "netplan_file": str(path),
            "interface": iface,
            "ip": ip_value,
            "gateway": gateway_value,
            "changed": True,
            "write": write_result,
            "apply": apply_result,
        }), http_code

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logger.exception(f"设置网口地址失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/workstations/<ws_id>/activate")
def workstation_activate_api(ws_id: str):
    """激活工位（验证密码后生成 device.json）"""
    try:
        logger.info(f"收到激活工位 {ws_id} 请求")
        
        # 获取密码
        if not request.is_json:
            error_msg = "请求内容不是 JSON 格式"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        data = request.get_json()
        password = data.get("password", "")
        
        if not password:
            error_msg = "未提供密码"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 400
        
        # 验证密码（使用 sudo -S 验证）
        logger.info(f"[工位 {ws_id}] 验证密码...")
        try:
            result = subprocess.run(
                ["sudo", "-S", "-k", "true"],
                input=password + "\n",
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                error_msg = "密码验证失败"
                logger.error(f"[工位 {ws_id}] {error_msg}")
                return jsonify({"ok": False, "error": error_msg}), 401
            
            logger.info(f"[工位 {ws_id}] 密码验证成功")
        except subprocess.TimeoutExpired:
            error_msg = "密码验证超时"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 500
        except Exception as e:
            error_msg = f"密码验证时发生异常: {str(e)}"
            logger.exception(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 500
        
        # 检查工位目录
        ws_dir = EDGE_SERVER_DIR / ws_id
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 检查 generate_device_config.py 脚本
        generate_script = ws_dir / "generate_device_config.pyc"
        if not generate_script.exists():
            error_msg = f"生成脚本不存在: {generate_script}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 执行生成脚本
        logger.info(f"[工位 {ws_id}] 执行生成脚本: {generate_script}")
        try:
            result = subprocess.run(
                ["python3", str(generate_script)],
                cwd=str(ws_dir),
                capture_output=True,
                text=True,
                timeout=30
            )
            
            logger.info(f"[工位 {ws_id}] 脚本返回码: {result.returncode}")
            
            if result.stdout:
                logger.info(f"[工位 {ws_id}] 脚本输出:\n{result.stdout}")
            
            if result.returncode == 0:
                # 验证 device.json 是否生成
                device_file = ws_dir / "device.json"
                if device_file.exists():
                    logger.info(f"[工位 {ws_id}] 激活成功，device.json 已生成")
                    return jsonify({
                        "ok": True,
                        "message": f"工位 {ws_id} 激活成功"
                    }), 200
                else:
                    error_msg = "脚本执行成功但 device.json 未生成"
                    logger.error(f"[工位 {ws_id}] {error_msg}")
                    return jsonify({"ok": False, "error": error_msg}), 500
            else:
                error_msg = f"脚本执行失败 (返回码: {result.returncode})"
                if result.stderr:
                    error_msg += f"\n错误输出: {result.stderr}"
                logger.error(f"[工位 {ws_id}] {error_msg}")
                return jsonify({"ok": False, "error": error_msg}), 500
        
        except subprocess.TimeoutExpired:
            error_msg = "脚本执行超时（超过 30 秒）"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 500
        except Exception as e:
            error_msg = f"执行脚本时发生异常: {str(e)}"
            logger.exception(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 500
    
    except Exception as e:
        error_msg = f"激活工位时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

@app.post("/workstations/<ws_id>/delete")
def workstation_delete_api(ws_id: str):
    """删除工位（只能删除最大ID的工位，且不能删除0号工位）"""
    try:
        logger.info(f"收到删除工位 {ws_id} 请求")
        
        # 检查是否是0号工位
        if ws_id == "0":
            error_msg = "不能删除0号工位"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 403
        
        # 获取所有工位
        workstations = scan_workstations()
        
        if not workstations:
            error_msg = "未找到任何工位"
            logger.error(error_msg)
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 找到最大ID
        max_id = max([int(ws["id"]) for ws in workstations])
        
        # 检查是否是最大ID
        if int(ws_id) != max_id:
            error_msg = f"只能删除最大ID的工位（当前最大ID: {max_id}），无法删除工位 {ws_id}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 403
        
        # 检查工位目录
        ws_dir = EDGE_SERVER_DIR / ws_id
        if not ws_dir.exists():
            error_msg = f"工位目录不存在: {ws_dir}"
            logger.error(f"[工位 {ws_id}] {error_msg}")
            return jsonify({"ok": False, "error": error_msg}), 404
        
        # 删除工位目录
        logger.info(f"[工位 {ws_id}] 删除工位目录: {ws_dir}")
        shutil.rmtree(ws_dir)
        logger.info(f"[工位 {ws_id}] 工位删除成功")
        
        return jsonify({
            "ok": True,
            "message": f"工位 {ws_id} 删除成功"
        }), 200
    
    except Exception as e:
        error_msg = f"删除工位时发生异常: {str(e)}"
        logger.exception(error_msg)
        return jsonify({"ok": False, "error": error_msg}), 500

def auto_start(timeout_sec: int):
    logger.info("auto start on boot (async)")
    ok, msg = spawn_action("start", timeout_sec)
    logger.info(f"auto start: ok={ok}, msg={msg}")

def auto_create_symlinks():
    """服务启动时自动创建符号链接（会先清理所有旧链接）"""
    try:
        logger.info("=" * 60)
        logger.info("服务启动 - 初始化三色灯符号链接")
        logger.info("步骤: 1. 清理旧链接  2. 扫描设备  3. 创建新链接")
        logger.info("-" * 60)
        
        global light_manager
        if light_manager is None:
            light_manager = get_manager()
        
        # 这个方法会先清理所有旧的符号链接，然后重新创建
        success, message = light_manager.create_symlinks_manually()
        
        if success:
            logger.info("-" * 60)
            logger.info(f"✓ {message}")
        else:
            logger.warning(f"⚠ {message}")
            logger.warning("可能原因: USB 设备未连接或权限不足")
        
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"自动创建符号链接失败: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--log", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # 方案A：logger 只在这里初始化一次
    global logger
    logger = setup_logger(
        args.log,
        level=logging.DEBUG if args.debug else logging.INFO
    )

    # 服务启动时自动创建符号链接（基于 USB 物理端口）
    auto_create_symlinks()

    # 服务启动时异步执行一次 start
    auto_start(args.timeout)

    logger.info(f"listen on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)

if __name__ == "__main__":
    main()
