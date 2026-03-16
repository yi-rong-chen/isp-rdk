#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import requests
import threading
import subprocess
import re
import json
from pathlib import Path

# Manager 服务配置
MANAGER_HOST = "localhost"
MANAGER_PORT = "8080"
MANAGER_BASE_URL = f"http://{MANAGER_HOST}:{MANAGER_PORT}"

# 获取脚本所在目录
SCRIPT_DIR = Path(__file__).resolve().parent
EDGE_BROWSER_SCRIPT = SCRIPT_DIR / "edge-browser.sh"


class ConfigEditorWindow:
    """配置编辑器窗口"""
    def __init__(self, parent, ws_id, main_app, base_url):
        self.ws_id = ws_id
        self.main_app = main_app
        self.base_url = base_url
        self.config_data = {}
        self.config_entries = {}

        # 创建新窗口
        self.window = tk.Toplevel(parent)
        self.window.title(f"配置编辑 - 工位 {ws_id}")
        self.window.geometry("1000x700")
        self.window.resizable(True, True)

        # 创建界面
        self.create_widgets()

        # 加载配置
        self.load_config()

    def create_widgets(self):
        """创建界面组件"""
        # 顶部工具栏
        toolbar = tk.Frame(self.window, padx=10, pady=5)
        toolbar.pack(fill=tk.X)

        tk.Label(
            toolbar,
            text=f"工位 {self.ws_id} 配置文件 (isp.json)",
            font=("Arial", 12, "bold")
        ).pack(side=tk.LEFT)

        # 保存按钮
        self.save_btn = tk.Button(
            toolbar,
            text="保存配置",
            command=self.save_config,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 10),
            width=10,
            cursor="hand2"
        )
        self.save_btn.pack(side=tk.RIGHT, padx=5)

        # 刷新按钮
        self.refresh_btn = tk.Button(
            toolbar,
            text="刷新",
            command=self.load_config,
            bg="#2196F3",
            fg="white",
            font=("Arial", 10),
            width=8,
            cursor="hand2"
        )
        self.refresh_btn.pack(side=tk.RIGHT, padx=5)

        # 配置列表容器（带滚动条）
        list_frame = tk.Frame(self.window, padx=10, pady=5)
        list_frame.pack(fill=tk.BOTH, expand=True)

        # 创建 Canvas 和 Scrollbar
        canvas = tk.Canvas(list_frame)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        self.config_list_frame = tk.Frame(canvas)

        self.config_list_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self.config_list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 状态栏
        self.status_label = tk.Label(
            self.window,
            text="就绪",
            font=("Arial", 9),
            fg="gray",
            anchor="w",
            padx=10,
            pady=5
        )
        self.status_label.pack(fill=tk.X)

    def load_config(self):
        """加载配置文件"""
        self.update_status("正在加载配置...", "blue")
        self.set_buttons_state(False)

        def do_load():
            try:
                # 调用 API 获取配置
                url = f"{self.base_url}/workstations/{self.ws_id}/get_config"
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()

                if data.get("ok"):
                    self.config_data = data.get("config", {})
                    self.main_app.log(f"✓ 工位 {self.ws_id} 配置加载成功")
                    self.update_status(f"配置已加载 ({len(self.config_data)} 项)", "green")
                    self.display_config()
                else:
                    error = data.get("error", "未知错误")
                    self.main_app.log(f"✗ 加载配置失败: {error}")
                    self.update_status(f"加载失败: {error}", "red")
                    # 在线程中调用主窗口封装的 messagebox（主线程安全）
                    self.main_app.show_error("错误", f"加载配置失败:\n{error}")
            except Exception as e:
                self.main_app.log(f"✗ 加载配置失败: {e}")
                self.update_status(f"加载失败: {str(e)}", "red")
                self.main_app.show_error("错误", f"加载配置失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_load, daemon=True)
        thread.start()

    def display_config(self):
        """显示配置项"""
        # 清空现有内容
        for widget in self.config_list_frame.winfo_children():
            widget.destroy()
        self.config_entries.clear()

        if not self.config_data:
            no_config_label = tk.Label(
                self.config_list_frame,
                text="没有配置项",
                font=("Arial", 10),
                fg="gray"
            )
            no_config_label.pack(pady=20)
            return

        # 显示每个配置项
        for key, value in sorted(self.config_data.items()):
            self.create_config_row(key, value)

    def create_config_row(self, key, value):
        """创建配置项行"""
        row_frame = tk.Frame(self.config_list_frame, pady=5)
        row_frame.pack(fill=tk.X, padx=5)

        # Key 标签
        key_label = tk.Label(
            row_frame,
            text=key,
            font=("Arial", 9, "bold"),
            width=25,
            anchor="w"
        )
        key_label.pack(side=tk.LEFT, padx=5)

        # Value 输入框
        value_frame = tk.Frame(row_frame)
        value_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # 判断值的类型
        value_str = str(value)

        # 如果值较长或包含换行，使用 Text 组件
        if len(value_str) > 100 or '\n' in value_str or '\r' in value_str:
            # 计算行数
            lines = value_str.count('\n') + value_str.count('\r\n') + 1
            height = min(max(3, lines), 10)

            value_text = tk.Text(
                value_frame,
                height=height,
                font=("Courier", 9),
                wrap=tk.WORD
            )
            value_text.insert("1.0", value_str)
            value_text.pack(fill=tk.X, expand=True)

            # 添加滚动条
            if height >= 10:
                text_scrollbar = tk.Scrollbar(value_frame, command=value_text.yview)
                text_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
                value_text.config(yscrollcommand=text_scrollbar.set)

            self.config_entries[key] = value_text
        else:
            # 使用 Entry 组件
            value_entry = tk.Entry(
                value_frame,
                font=("Courier", 9)
            )
            value_entry.insert(0, value_str)
            value_entry.pack(fill=tk.X, expand=True)

            self.config_entries[key] = value_entry

    def save_config(self):
        """保存配置"""
        if not messagebox.askyesno(
            "确认",
            f"确定要保存工位 {self.ws_id} 的配置吗？\n保存后将自动重启该工位。"
        ):
            return

        self.update_status("正在保存配置...", "blue")
        self.set_buttons_state(False)

        def do_save():
            try:
                # 收集所有配置项
                new_config = {}
                for key, widget in self.config_entries.items():
                    if isinstance(widget, tk.Text):
                        value = widget.get("1.0", tk.END).strip()
                    else:
                        value = widget.get().strip()
                    new_config[key] = value

                self.main_app.log(f"\n保存工位 {self.ws_id} 配置...")
                self.main_app.log(f"配置项数量: {len(new_config)}")

                # 调用 API 保存配置
                url = f"{self.base_url}/workstations/{self.ws_id}/save_config"
                response = requests.post(
                    url,
                    json={"config": new_config},
                    timeout=60
                )
                response.raise_for_status()
                data = response.json()

                if data.get("ok"):
                    message = data.get("message", "保存成功")
                    self.main_app.log(f"✓ 工位 {self.ws_id}: {message}")
                    self.update_status("保存成功", "green")
                    # 使用主窗口的封装，避免子线程直接弹框
                    self.main_app.show_info(
                        "操作成功",
                        f"工位 {self.ws_id} 配置保存成功\n配置已生效并已重启服务"
                    )
                    # 关闭窗口
                    self.window.destroy()
                else:
                    error = data.get("error", "未知错误")
                    self.main_app.log(f"✗ 工位 {self.ws_id}: {error}")
                    self.update_status("保存失败", "red")
                    self.main_app.show_error("错误", f"保存配置失败:\n{error}")
            except Exception as e:
                self.main_app.log(f"✗ 保存配置失败: {e}")
                self.update_status(f"保存失败: {str(e)}", "red")
                self.main_app.show_error("错误", f"保存配置失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_save, daemon=True)
        thread.start()

    def update_status(self, text, color="gray"):
        """更新状态标签"""
        self.status_label.config(text=text, fg=color)

    def set_buttons_state(self, enabled):
        """设置按钮状态"""
        state = tk.NORMAL if enabled else tk.DISABLED
        self.save_btn.config(state=state)
        self.refresh_btn.config(state=state)


class ServiceControlApp:
    def __init__(self, root):
        self.root = root
        self.root.title("服务控制面板")
        self.root.geometry("900x900")
        self.root.resizable(True, True)

        # ✅ 统一设置 messagebox 样式，避免文字竖着挤在一起
        self.root.option_add('*Dialog.msg.font', 'TkDefaultFont 11')
        # 对话框内容区域宽度（单位：像素），可根据需要加大或缩小
        self.root.option_add('*Dialog.msg.wrapLength', '400')

        # 设置窗口居中
        self.center_window()

        # 创建界面
        self.create_widgets()

        # 状态变量
        self.is_processing = False
        self.lights_list = []
        self.light_test_buttons = {}  # 存储每个灯的测试按钮
        self.workstations_list = []
        self.workstation_buttons = {}  # 存储每个工位的按钮
        self.netplan_rows = {}  # 存储网口行组件 {iface: {"entry": Entry, "btn": Button}}

        # 启动时检查三色灯状态
        self.root.after(500, self.check_lights_on_startup)

        # 启动时加载工位列表
        self.root.after(1000, self.load_workstations_list)

        # 启动时检查系统服务
        self.root.after(1500, self.check_system_services_on_startup)

        # 启动时加载网口配置（netplan）
        self.root.after(2000, self.load_netplan_interfaces)

        # 启动时检查 FRP 配置是否需要初始化
        self.root.after(1700, self.load_frp_status)

    def center_window(self):
        """将窗口居中显示"""
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')

    def create_widgets(self):
        """创建界面组件"""
        # 主容器
        main_frame = tk.Frame(self.root, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)


        # 服务控制区域
        service_frame = tk.LabelFrame(main_frame, text="服务控制", padx=10, pady=10)
        service_frame.pack(fill=tk.X, pady=5)

        button_frame = tk.Frame(service_frame)
        button_frame.pack()

        # 重启按钮
        self.restart_btn = tk.Button(
            button_frame,
            text="重启所有服务",
            command=self.restart_services,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 12),
            width=12,
            height=1,
            cursor="hand2"
        )
        self.restart_btn.pack(side=tk.LEFT, padx=5)

        # 停止按钮
        self.stop_btn = tk.Button(
            button_frame,
            text="停止所有服务",
            command=self.stop_services,
            bg="#f44336",
            fg="white",
            font=("Arial", 12),
            width=12,
            height=1,
            cursor="hand2"
        )
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        # 三色灯控制区域
        lights_frame = tk.LabelFrame(main_frame, text="三色灯管理", padx=10, pady=10)
        lights_frame.pack(fill=tk.X, pady=5)
        
        # 三色灯状态显示
        status_frame = tk.Frame(lights_frame)
        status_frame.pack(fill=tk.X, pady=5)
        
        self.lights_status_label = tk.Label(
            status_frame,
            text="检查中...",
            font=("Arial", 10),
            fg="gray",
            anchor="w"
        )
        self.lights_status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # 刷新按钮
        self.refresh_lights_btn = tk.Button(
            status_frame,
            text="刷新",
            command=self.refresh_lights_list,
            bg="#2196F3",
            fg="white",
            font=("Arial", 9),
            width=8,
            cursor="hand2"
        )
        self.refresh_lights_btn.pack(side=tk.RIGHT, padx=2)
        
        # 初始化按钮
        self.init_lights_btn = tk.Button(
            status_frame,
            text="初始化",
            command=self.initialize_lights,
            bg="#FF9800",
            fg="white",
            font=("Arial", 9),
            width=8,
            cursor="hand2"
        )
        self.init_lights_btn.pack(side=tk.RIGHT, padx=2)
        
        # 三色灯列表容器（带滚动条）
        list_container = tk.Frame(lights_frame)
        list_container.pack(fill=tk.BOTH, expand=True, pady=5)
        
        # 创建 Canvas 和 Scrollbar
        canvas = tk.Canvas(list_container, height=80)
        self.lights_canvas = canvas
        scrollbar = tk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.lights_list_frame = tk.Frame(canvas)
        
        self.lights_list_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=self.lights_list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 工位管理区域
        workstations_frame = tk.LabelFrame(main_frame, text="工位管理", padx=10, pady=10)
        workstations_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # 工位状态显示
        ws_status_frame = tk.Frame(workstations_frame)
        ws_status_frame.pack(fill=tk.X, pady=5)

        self.workstations_status_label = tk.Label(
            ws_status_frame,
            text="检查中...",
            font=("Arial", 10),
            fg="gray",
            anchor="w"
        )
        self.workstations_status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 刷新工位按钮
        self.refresh_workstations_btn = tk.Button(
            ws_status_frame,
            text="刷新",
            command=self.refresh_workstations_list,
            bg="#2196F3",
            fg="white",
            font=("Arial", 9),
            width=8,
            cursor="hand2"
        )
        self.refresh_workstations_btn.pack(side=tk.RIGHT, padx=2)

        # 工位列表容器（带滚动条）
        ws_list_container = tk.Frame(workstations_frame)
        ws_list_container.pack(fill=tk.BOTH, expand=True, pady=5)

        # 创建 Canvas 和 Scrollbar（增加高度以显示更多内容）
        ws_canvas = tk.Canvas(ws_list_container, height=120)
        ws_scrollbar = tk.Scrollbar(ws_list_container, orient="vertical", command=ws_canvas.yview)
        self.workstations_list_frame = tk.Frame(ws_canvas)

        self.workstations_list_frame.bind(
            "<Configure>",
            lambda e: ws_canvas.configure(scrollregion=ws_canvas.bbox("all"))
        )

        ws_canvas.create_window((0, 0), window=self.workstations_list_frame, anchor="nw")
        ws_canvas.configure(yscrollcommand=ws_scrollbar.set)

        ws_canvas.pack(side="left", fill="both", expand=True)
        ws_scrollbar.pack(side="right", fill="y")

        # 系统检查服务区域
        system_check_frame = tk.LabelFrame(main_frame, text="系统检查服务", padx=10, pady=10)
        system_check_frame.pack(fill=tk.X, pady=5)

        # FRPC 服务检查
        frpc_check_frame = tk.Frame(system_check_frame)
        frpc_check_frame.pack(fill=tk.X, pady=3)

        frpc_label = tk.Label(
            frpc_check_frame,
            text="FRPC 服务状态:",
            font=("Arial", 10),
            width=20,
            anchor="w"
        )
        frpc_label.pack(side=tk.LEFT, padx=5)

        self.frpc_status_label = tk.Label(
            frpc_check_frame,
            text="检查中...",
            font=("Arial", 10),
            fg="gray",
            anchor="w"
        )
        self.frpc_status_label.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        frpc_check_btn = tk.Button(
            frpc_check_frame,
            text="检查",
            command=self.check_frpc_service,
            bg="#2196F3",
            fg="white",
            font=("Arial", 9),
            width=8,
            cursor="hand2"
        )
        frpc_check_btn.pack(side=tk.RIGHT, padx=2)

        # FRP 初始化设置（仅在模板时可用）
        frp_init_frame = tk.Frame(system_check_frame)
        frp_init_frame.pack(fill=tk.X, pady=3)

        frp_label = tk.Label(
            frp_init_frame,
            text="FRP 初始化端口:",
            font=("Arial", 10),
            width=20,
            anchor="w"
        )
        frp_label.pack(side=tk.LEFT, padx=5)

        self.frp_init_status_label = tk.Label(
            frp_init_frame,
            text="检查中...",
            font=("Arial", 10),
            fg="gray",
            anchor="w"
        )
        self.frp_init_status_label.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        self.frp_port_entry = tk.Entry(frp_init_frame, width=10, font=("Arial", 10))
        self.frp_port_entry.pack(side=tk.LEFT, padx=5)

        self.frp_init_btn = tk.Button(
            frp_init_frame,
            text="初始化",
            command=self.initialize_frp,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 9),
            width=8,
            cursor="hand2"
        )
        self.frp_init_btn.pack(side=tk.RIGHT, padx=2)

        # 网口设置（netplan）
        netplan_frame = tk.LabelFrame(main_frame, text="网口设置 (netplan)", padx=10, pady=10)
        netplan_frame.pack(fill=tk.X, pady=5)

        netplan_top = tk.Frame(netplan_frame)
        netplan_top.pack(fill=tk.X, pady=3)

        self.netplan_status_label = tk.Label(
            netplan_top,
            text="状态: 检查中...",
            font=("Arial", 10),
            fg="gray",
            anchor="w"
        )
        self.netplan_status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.refresh_netplan_btn = tk.Button(
            netplan_top,
            text="刷新",
            command=self.load_netplan_interfaces,
            bg="#2196F3",
            fg="white",
            font=("Arial", 9),
            width=8,
            cursor="hand2"
        )
        self.refresh_netplan_btn.pack(side=tk.RIGHT, padx=2)

        # 网口列表容器（可滚动）
        netplan_list_container = tk.Frame(netplan_frame)
        netplan_list_container.pack(fill=tk.BOTH, expand=True, pady=5)

        netplan_canvas = tk.Canvas(netplan_list_container)
        self.netplan_canvas = netplan_canvas
        netplan_scrollbar = tk.Scrollbar(netplan_list_container, orient="vertical", command=netplan_canvas.yview)
        self.netplan_list_frame = tk.Frame(netplan_canvas)

        self.netplan_list_frame.bind(
            "<Configure>",
            lambda e: netplan_canvas.configure(scrollregion=netplan_canvas.bbox("all"))
        )

        netplan_canvas.create_window((0, 0), window=self.netplan_list_frame, anchor="nw")
        netplan_canvas.configure(yscrollcommand=netplan_scrollbar.set)

        netplan_canvas.pack(side="left", fill="both", expand=True)
        netplan_scrollbar.pack(side="right", fill="y")

        # 状态标签
        self.status_label = tk.Label(
            main_frame,
            text="就绪",
            font=("Arial", 11),
            fg="gray",
            pady=5
        )
        self.status_label.pack()

        # 日志输出区域
        log_frame = tk.LabelFrame(main_frame, text="操作日志", padx=5, pady=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            width=75,
            font=("Courier", 9),
            wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 初始日志
        self.log("服务控制面板已启动")
        self.log(f"Manager 服务地址: {MANAGER_BASE_URL}")

    def log(self, message):
        """添加日志消息"""
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    # ========= 主线程安全的 messagebox 封装 =========
    def show_info(self, title, message):
        """在主线程弹出信息提示框"""
        def _show():
            messagebox.showinfo(title=title, message=message, parent=self.root)
        self.root.after(0, _show)

    def show_error(self, title, message):
        """在主线程弹出错误提示框"""
        def _show():
            messagebox.showerror(title=title, message=message, parent=self.root)
        self.root.after(0, _show)

    def show_warning(self, title, message):
        """在主线程弹出警告提示框"""
        def _show():
            messagebox.showwarning(title=title, message=message, parent=self.root)
        self.root.after(0, _show)
    # ==========================================

    def update_status(self, text, color="gray"):
        """更新状态标签"""
        self.status_label.config(text=text, fg=color)

    def set_buttons_state(self, enabled):
        """设置按钮状态"""
        state = tk.NORMAL if enabled else tk.DISABLED
        self.restart_btn.config(state=state)
        self.stop_btn.config(state=state)
        self.refresh_lights_btn.config(state=state)
        self.init_lights_btn.config(state=state)
        self.refresh_workstations_btn.config(state=state)
        if hasattr(self, "refresh_netplan_btn"):
            self.refresh_netplan_btn.config(state=state)
        if hasattr(self, "frp_init_btn"):
            self.frp_init_btn.config(state=state)
        if hasattr(self, "frp_port_entry"):
            try:
                self.frp_port_entry.config(state=(tk.NORMAL if enabled else tk.DISABLED))
            except Exception:
                pass
        # 设置所有测试按钮的状态
        for btn in self.light_test_buttons.values():
            btn.config(state=state)
        # 设置所有工位按钮的状态
        for ws_btns in self.workstation_buttons.values():
            for btn in ws_btns.values():
                btn.config(state=state)
        # 网口输入框和 apply 按钮
        for row in self.netplan_rows.values():
            try:
                if "ip_entry" in row:
                    row.get("ip_entry").config(state=state)
                if "gateway_entry" in row:
                    row.get("gateway_entry").config(state=state)
                if "entry" in row:  # 兼容旧版本
                    row.get("entry").config(state=state)
                row.get("btn").config(state=state)
            except Exception:
                pass
        self.is_processing = not enabled

    def call_manager_api(self, endpoint, method="POST", params=None, json_data=None):
        """调用 Manager 服务的 API"""
        url = f"{MANAGER_BASE_URL}/{endpoint}"
        try:
            if method.upper() == "GET":
                response = requests.get(url, params=params, timeout=10)
            else:
                if json_data is not None:
                    response = requests.post(url, json=json_data, timeout=10)
                else:
                    response = requests.post(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            return data
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"连接失败: {str(e)}"}

    def call_manager_api_json(self, endpoint, payload, method="POST", timeout=60):
        """调用 Manager 服务的 API（JSON body）"""
        url = f"{MANAGER_BASE_URL}/{endpoint}"
        try:
            if method.upper() == "GET":
                response = requests.get(url, params=payload, timeout=timeout)
            else:
                response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"连接失败: {str(e)}"}

    def kill_chromium_browser(self):
        """停止所有 chromium-browser 进程"""
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                lines = result.stdout.split('\n')
                pids = []
                for line in lines:
                    if 'chromium-browser' in line and 'grep' not in line:
                        parts = line.split()
                        if len(parts) > 1:
                            pids.append(parts[1])

                if pids:
                    self.log(f"找到 {len(pids)} 个 chromium-browser 进程")
                    for pid in pids:
                        try:
                            subprocess.run(
                                ["kill", "-9", pid],
                                capture_output=True,
                                timeout=2
                            )
                            self.log(f"  - 已停止进程 (PID: {pid})")
                        except Exception as e:
                            self.log(f"  - 停止进程失败 (PID: {pid}): {e}")
                    return True
                else:
                    self.log("未找到运行中的 chromium-browser 进程")
                    return True
            return False
        except Exception as e:
            self.log(f"检查 chromium-browser 进程时出错: {e}")
            return False

    def start_edge_browser(self):
        """启动 edge-browser"""
        if EDGE_BROWSER_SCRIPT.exists() and EDGE_BROWSER_SCRIPT.is_file():
            try:
                self.log("启动 edge-browser...")
                subprocess.Popen(
                    ["/bin/bash", str(EDGE_BROWSER_SCRIPT)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self.log("edge-browser 启动命令已执行")
                return True
            except Exception as e:
                self.log(f"启动 edge-browser 失败: {e}")
                return False
        else:
            self.log(f"警告: 找不到 edge-browser.sh 脚本: {EDGE_BROWSER_SCRIPT}")
            return False

    def restart_services(self):
        """重启服务"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        # 确认对话框
        if not messagebox.askyesno("确认", "确定要重启所有服务吗？"):
            return

        self.set_buttons_state(False)
        self.update_status("正在重启服务...", "blue")
        self.log("\n" + "=" * 50)
        self.log("开始重启服务...")

        def do_restart():
            try:
                # 步骤1: 停止 chromium-browser
                self.log("停止 chromium-browser 进程...")
                self.kill_chromium_browser()

                # 步骤2: 调用 manager 服务的 start 接口
                self.log("调用 manager 服务的 start 接口...")
                data = self.call_manager_api("start")

                if data.get("ok"):
                    self.log("✓ Manager start 接口调用成功")
                else:
                    msg = data.get("message", data.get("error", "未知错误"))
                    self.log(f"⚠ Manager start 接口调用失败: {msg}")
                    self.log("继续执行后续步骤...")

                # 等待一段时间
                self.log("等待 5 秒...")
                threading.Event().wait(5)

                # 步骤3: 启动 edge-browser
                self.start_edge_browser()

                self.log("=" * 50)
                self.log("重启操作完成")
                self.update_status("重启完成", "green")

                self.show_info("操作成功", "所有服务重启操作已完成")
            except Exception as e:
                self.log(f"错误: {e}")
                self.update_status("重启失败", "red")
                self.show_error("错误", f"重启服务时发生错误:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        # 在后台线程执行
        thread = threading.Thread(target=do_restart, daemon=True)
        thread.start()

    def stop_services(self):
        """停止服务"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        # 确认对话框
        if not messagebox.askyesno("确认", "确定要停止所有服务吗？"):
            return

        self.set_buttons_state(False)
        self.update_status("正在停止服务...", "blue")
        self.log("\n" + "=" * 50)
        self.log("开始停止服务...")

        def do_stop():
            try:
                # 步骤1: 调用 manager 服务的 stop 接口
                self.log("调用 manager 服务的 stop 接口...")
                data = self.call_manager_api("stop")

                if data.get("ok"):
                    self.log("✓ Manager stop 接口调用成功")
                else:
                    msg = data.get("message", data.get("error", "未知错误"))
                    self.log(f"⚠ Manager stop 接口调用失败: {msg}")

                # 步骤2: 停止 chromium-browser
                self.log("停止 chromium-browser 进程...")
                self.kill_chromium_browser()

                self.log("=" * 50)
                self.log("停止操作完成")
                self.update_status("停止完成", "green")

                self.show_info("操作成功", "所有服务停止操作已完成")
            except Exception as e:
                self.log(f"错误: {e}")
                self.update_status("停止失败", "red")
                self.show_error("错误", f"停止服务时发生错误:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        # 在后台线程执行
        thread = threading.Thread(target=do_stop, daemon=True)
        thread.start()

    def load_workstations_list(self):
        """加载工位列表"""

        def do_load():
            self.log("\n加载工位列表...")
            data = self.call_manager_api("workstations/list", method="GET")

            if data.get("ok"):
                self.workstations_list = data.get("workstations", [])
                count = data.get("count", 0)
                self.log(f"找到 {count} 个工位")
                self.update_workstations_list_display()
                self.update_workstations_status_display(f"找到 {count} 个工位", "green")
            else:
                error = data.get("error", "未知错误")
                self.log(f"加载工位列表失败: {error}")
                self.workstations_list = []
                self.update_workstations_list_display()
                self.update_workstations_status_display("加载失败", "red")

        thread = threading.Thread(target=do_load, daemon=True)
        thread.start()

    def update_workstations_list_display(self):
        """更新工位列表显示"""
        # 清空现有列表
        for widget in self.workstations_list_frame.winfo_children():
            widget.destroy()
        self.workstation_buttons.clear()

        if not self.workstations_list:
            no_ws_label = tk.Label(
                self.workstations_list_frame,
                text="未找到工位目录",
                font=("Arial", 10),
                fg="gray"
            )
            no_ws_label.pack(pady=20)
            return

        # 显示每个工位
        for ws in self.workstations_list:
            ws_id = ws.get("id")
            has_restart = ws.get("has_restart_script", False)
            has_config = ws.get("has_config", False)
            has_model = ws.get("has_model", False)
            is_activated = ws.get("is_activated", False)

            # 创建每个工位的行
            ws_row = tk.Frame(self.workstations_list_frame, pady=5)
            ws_row.pack(fill=tk.X, padx=5)

            # 状态指示器
            if is_activated:
                status_color = "green" if has_restart else "orange"
            else:
                status_color = "red"
            status_canvas = tk.Canvas(ws_row, width=10, height=10)
            status_canvas.create_oval(2, 2, 10, 10, fill=status_color, outline="")
            status_canvas.pack(side=tk.LEFT, padx=5)

            # 工位名称
            name_label = tk.Label(
                ws_row,
                text=f"{ws_id} 工位",
                font=("Arial", 11, "bold"),
                width=10,
                anchor="w"
            )
            name_label.pack(side=tk.LEFT, padx=5)

            # 状态信息
            status_parts = []
            if not is_activated:
                status_parts.append("未激活")
            else:
                if has_config:
                    status_parts.append("配置✓")
                if has_model:
                    status_parts.append("模型✓")
            status_text = " ".join(status_parts) if status_parts else "未配置"

            status_label = tk.Label(
                ws_row,
                text=status_text,
                font=("Arial", 9),
                fg="red" if not is_activated else "gray",
                anchor="w",
                width=15
            )
            status_label.pack(side=tk.LEFT, padx=5)

            # 按钮容器（左对齐，填充剩余空间）
            btn_frame = tk.Frame(ws_row)
            btn_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

            # 如果未激活，显示激活按钮
            if not is_activated:
                activate_btn = tk.Button(
                    btn_frame,
                    text="激活工位",
                    command=lambda wid=ws_id: self.activate_workstation(wid),
                    bg="#4CAF50",
                    fg="white",
                    font=("Arial", 9),
                    width=8,
                    cursor="hand2"
                )
                activate_btn.pack(side=tk.LEFT, padx=2)
                
                # 删除按钮（只有未激活的工位才能删除，且不能删除0号工位）
                if ws_id != "0":
                    # 检查是否是最大ID
                    all_ids = [int(w["id"]) for w in self.workstations_list]
                    max_id = max(all_ids)
                    can_delete = int(ws_id) == max_id
                    
                    delete_btn = tk.Button(
                        btn_frame,
                        text="删除工位",
                        command=lambda wid=ws_id: self.delete_workstation(wid),
                        bg="#f44336",
                        fg="white",
                        font=("Arial", 9),
                        width=8,
                        cursor="hand2",
                        state=tk.NORMAL if can_delete else tk.DISABLED
                    )
                    delete_btn.pack(side=tk.LEFT, padx=2)
                    
                    self.workstation_buttons[ws_id] = {
                        "activate": activate_btn,
                        "delete": delete_btn
                    }
                else:
                    self.workstation_buttons[ws_id] = {
                        "activate": activate_btn
                    }
            else:
                # 服务重启按钮
                restart_btn = tk.Button(
                    btn_frame,
                    text="服务重启",
                    command=lambda wid=ws_id: self.restart_workstation(wid),
                    bg="#4CAF50",
                    fg="white",
                    font=("Arial", 9),
                    width=8,
                    cursor="hand2",
                    state=tk.NORMAL if has_restart else tk.DISABLED
                )
                restart_btn.pack(side=tk.LEFT, padx=2)

                # 服务停止按钮
                stop_btn = tk.Button(
                    btn_frame,
                    text="服务停止",
                    command=lambda wid=ws_id: self.stop_workstation(wid),
                    bg="#f44336",
                    fg="white",
                    font=("Arial", 9),
                    width=8,
                    cursor="hand2",
                    state=tk.NORMAL if has_restart else tk.DISABLED
                )
                stop_btn.pack(side=tk.LEFT, padx=2)

                # 配置修改按钮
                config_edit_btn = tk.Button(
                    btn_frame,
                    text="配置修改",
                    command=lambda wid=ws_id: self.edit_workstation_config(wid),
                    bg="#9C27B0",
                    fg="white",
                    font=("Arial", 9),
                    width=10,
                    cursor="hand2",
                    state=tk.NORMAL if has_config else tk.DISABLED
                )
                config_edit_btn.pack(side=tk.LEFT, padx=2)

                # 配置回滚按钮
                config_rollback_btn = tk.Button(
                    btn_frame,
                    text="配置回滚",
                    command=lambda wid=ws_id: self.rollback_workstation_config(wid),
                    bg="#FF5722",
                    fg="white",
                    font=("Arial", 9),
                    width=10,
                    cursor="hand2",
                    state=tk.NORMAL if has_config else tk.DISABLED
                )
                config_rollback_btn.pack(side=tk.LEFT, padx=2)

                # 配置替换按钮
                config_btn = tk.Button(
                    btn_frame,
                    text="配置替换",
                    command=lambda wid=ws_id: self.replace_workstation_config(wid),
                    bg="#2196F3",
                    fg="white",
                    font=("Arial", 9),
                    width=8,
                    cursor="hand2",
                    state=tk.NORMAL if has_restart else tk.DISABLED
                )
                config_btn.pack(side=tk.LEFT, padx=2)

                # 模型替换按钮
                model_btn = tk.Button(
                    btn_frame,
                    text="模型替换",
                    command=lambda wid=ws_id: self.replace_workstation_model(wid),
                    bg="#FF9800",
                    fg="white",
                    font=("Arial", 9),
                    width=8,
                    cursor="hand2",
                    state=tk.NORMAL if has_restart else tk.DISABLED
                )
                model_btn.pack(side=tk.LEFT, padx=2)

                # 删除按钮（0号工位不显示，只有最大ID的工位可用）
                if ws_id != "0":
                    # 检查是否是最大ID
                    all_ids = [int(w["id"]) for w in self.workstations_list]
                    max_id = max(all_ids)
                    can_delete = int(ws_id) == max_id
                    
                    delete_btn = tk.Button(
                        btn_frame,
                        text="删除工位",
                        command=lambda wid=ws_id: self.delete_workstation(wid),
                        bg="#9E9E9E",
                        fg="white",
                        font=("Arial", 9),
                        width=8,
                        cursor="hand2",
                        state=tk.NORMAL if can_delete else tk.DISABLED
                    )
                    delete_btn.pack(side=tk.LEFT, padx=2)
                    
                    # 保存按钮引用
                    self.workstation_buttons[ws_id] = {
                        "restart": restart_btn,
                        "stop": stop_btn,
                        "config_edit": config_edit_btn,
                        "config_rollback": config_rollback_btn,
                        "config": config_btn,
                        "model": model_btn,
                        "delete": delete_btn
                    }
                else:
                    # 保存按钮引用（0号工位没有删除按钮）
                    self.workstation_buttons[ws_id] = {
                        "restart": restart_btn,
                        "stop": stop_btn,
                        "config_edit": config_edit_btn,
                        "config_rollback": config_rollback_btn,
                        "config": config_btn,
                        "model": model_btn
                    }

    def update_workstations_status_display(self, text, color="gray"):
        """更新工位状态显示"""
        self.workstations_status_label.config(text=f"状态: {text}", fg=color)

    def refresh_workstations_list(self):
        """刷新工位列表"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        self.set_buttons_state(False)
        self.log("\n刷新工位列表...")

        def do_refresh():
            try:
                self.load_workstations_list()
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_refresh, daemon=True)
        thread.start()

    def restart_workstation(self, ws_id: str):
        """重启指定工位"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        if not messagebox.askyesno("确认", f"确定要重启 {ws_id} 工位吗？"):
            return

        self.set_buttons_state(False)
        self.update_status(f"正在重启工位 {ws_id}...", "blue")
        self.log(f"\n重启工位 {ws_id}...")

        def do_restart():
            try:
                data = self.call_manager_api(f"workstations/{ws_id}/restart")

                if data.get("ok"):
                    message = data.get("message", "重启成功")
                    self.log(f"✓ 工位 {ws_id}: {message}")
                    self.update_status("重启完成", "green")
                    self.show_info("操作成功", f"工位 {ws_id} 服务重启成功")
                else:
                    error = data.get("error", data.get("message", "未知错误"))
                    self.log(f"✗ 工位 {ws_id}: {error}")
                    self.update_status("重启失败", "red")
                    self.show_error("错误", f"工位 {ws_id} 重启失败:\n{error}")
            except Exception as e:
                self.log(f"✗ 重启失败: {e}")
                self.update_status("重启失败", "red")
                self.show_error("错误", f"重启失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_restart, daemon=True)
        thread.start()

    def stop_workstation(self, ws_id: str):
        """停止指定工位服务"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        if not messagebox.askyesno("确认", f"确定要停止 {ws_id} 工位的服务吗？"):
            return

        self.set_buttons_state(False)
        self.update_status(f"正在停止工位 {ws_id} 服务...", "blue")
        self.log(f"\n停止工位 {ws_id} 服务...")

        def do_stop():
            try:
                data = self.call_manager_api(f"workstations/{ws_id}/stop")

                if data.get("ok"):
                    message = data.get("message", "停止成功")
                    self.log(f"✓ 工位 {ws_id}: {message}")
                    self.update_status("停止完成", "green")
                    self.show_info("操作成功", f"工位 {ws_id} 服务停止成功")
                else:
                    error = data.get("error", data.get("message", "未知错误"))
                    self.log(f"✗ 工位 {ws_id}: {error}")
                    self.update_status("停止失败", "red")
                    self.show_error("错误", f"工位 {ws_id} 服务停止失败:\n{error}")
            except Exception as e:
                self.log(f"✗ 停止失败: {e}")
                self.update_status("停止失败", "red")
                self.show_error("错误", f"停止失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_stop, daemon=True)
        thread.start()

    def edit_workstation_config(self, ws_id: str):
        """编辑工位配置"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        self.log(f"\n打开工位 {ws_id} 配置编辑器...")

        # 创建配置编辑窗口
        ConfigEditorWindow(self.root, ws_id, self, MANAGER_BASE_URL)

    def rollback_workstation_config(self, ws_id: str):
        """回滚工位配置"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        self.set_buttons_state(False)
        self.log(f"\n检查工位 {ws_id} 备份文件...")

        def fetch_backups():
            try:
                data = self.call_manager_api(
                    f"workstations/{ws_id}/get_backups",
                    method="GET"
                )
            except Exception as e:
                data = {"ok": False, "error": str(e)}

            # 在主线程处理 UI（弹框）
            self.root.after(0, lambda: self._handle_rollback_backups(ws_id, data))

        thread = threading.Thread(target=fetch_backups, daemon=True)
        thread.start()

    def _handle_rollback_backups(self, ws_id: str, data: dict):
        """在主线程中处理回滚前的备份检查与确认"""
        try:
            if not data.get("ok"):
                error = data.get("error", "未知错误")
                self.log(f"✗ 检查备份文件失败: {error}")
                self.update_status("检查失败", "red")
                messagebox.showerror("错误", f"检查备份文件失败:\n{error}")
                self.set_buttons_state(True)
                return

            backups = data.get("backups", [])
            count = data.get("count", 0)

            if count == 0:
                self.log(f"⚠ 工位 {ws_id} 没有可用的备份文件")
                self.update_status("没有备份", "orange")
                messagebox.showwarning("提示", f"工位 {ws_id} 没有可用的备份文件")
                self.set_buttons_state(True)
                return

            latest_backup = backups[0]
            backup_filename = latest_backup.get("filename", "")

            self.log(f"找到 {count} 个备份文件")
            self.log(f"最新备份: {backup_filename}")

            if not messagebox.askyesno(
                "确认回滚",
                f"确定要回滚工位 {ws_id} 的配置吗？\n\n"
                f"将恢复到备份: {backup_filename}\n"
                f"共有 {count} 个备份文件\n\n"
                f"回滚后将自动重启该工位，并删除该备份文件。"
            ):
                self.log("用户取消回滚操作")
                self.set_buttons_state(True)
                return

            self.update_status(f"正在回滚工位 {ws_id} 配置...", "blue")
            self.log(f"\n开始回滚工位 {ws_id} 配置...")

            def do_rollback():
                try:
                    data2 = self.call_manager_api(
                        f"workstations/{ws_id}/rollback_config"
                    )

                    if data2.get("ok"):
                        message = data2.get("message", "回滚成功")
                        backup_file = data2.get("backup_file", "")
                        self.log(f"✓ 工位 {ws_id}: {message}")
                        if backup_file:
                            self.log(f"已删除备份文件: {backup_file}")
                        self.update_status("回滚完成", "green")
                        self.show_info(
                            "操作成功",
                            f"工位 {ws_id} 配置回滚成功\n配置已恢复并已重启服务"
                        )
                    else:
                        error = data2.get("error", "未知错误")
                        self.log(f"✗ 工位 {ws_id}: {error}")
                        self.update_status("回滚失败", "red")
                        self.show_error(
                            "错误",
                            f"工位 {ws_id} 配置回滚失败:\n{error}"
                        )
                except Exception as e:
                    self.log(f"✗ 回滚失败: {e}")
                    self.update_status("回滚失败", "red")
                    self.show_error("错误", f"回滚失败:\n{str(e)}")
                finally:
                    self.set_buttons_state(True)

            threading.Thread(target=do_rollback, daemon=True).start()
        except Exception as e:
            self.log(f"✗ 回滚失败: {e}")
            self.update_status("回滚失败", "red")
            self.show_error("错误", f"回滚失败:\n{str(e)}")
            self.set_buttons_state(True)

    def replace_workstation_config(self, ws_id: str):
        """替换工位配置文件"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        # 选择配置文件
        file_path = filedialog.askopenfilename(
            title=f"选择配置文件 (工位 {ws_id})",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )

        if not file_path:
            return

        if not messagebox.askyesno(
            "确认",
            f"确定要替换工位 {ws_id} 的配置文件吗？\n将自动重启该工位。"
        ):
            return

        self.set_buttons_state(False)
        self.update_status(f"正在替换工位 {ws_id} 配置...", "blue")
        self.log(f"\n替换工位 {ws_id} 配置文件...")
        self.log(f"文件: {file_path}")

        def do_replace():
            try:
                with open(file_path, 'rb') as f:
                    files = {'file': f}
                    url = f"{MANAGER_BASE_URL}/workstations/{ws_id}/replace_config"
                    response = requests.post(url, files=files, timeout=60)
                    response.raise_for_status()
                    data = response.json()

                if data.get("ok"):
                    message = data.get("message", "配置替换成功")
                    self.log(f"✓ 工位 {ws_id}: {message}")
                    self.update_status("配置替换完成", "green")
                    self.show_info(
                        "操作成功",
                        f"工位 {ws_id} 配置替换成功\n配置已生效并已重启服务"
                    )
                else:
                    error = data.get("error", "未知错误")
                    self.log(f"✗ 工位 {ws_id}: {error}")
                    self.update_status("配置替换失败", "red")
                    self.show_error(
                        "错误",
                        f"工位 {ws_id} 配置替换失败:\n{error}"
                    )
            except Exception as e:
                self.log(f"✗ 配置替换失败: {e}")
                self.update_status("配置替换失败", "red")
                self.show_error("错误", f"配置替换失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_replace, daemon=True)
        thread.start()

    def replace_workstation_model(self, ws_id: str):
        """替换工位模型文件"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        # 选择模型文件
        file_path = filedialog.askopenfilename(
            title=f"选择模型文件 (工位 {ws_id})",
            filetypes=[("Engine files", "*.engine"), ("All files", "*.*")]
        )

        if not file_path:
            return

        if not messagebox.askyesno(
            "确认",
            f"确定要替换工位 {ws_id} 的模型文件吗？\n将自动重启该工位。"
        ):
            return

        self.set_buttons_state(False)
        self.update_status(f"正在替换工位 {ws_id} 模型...", "blue")
        self.log(f"\n替换工位 {ws_id} 模型文件...")
        self.log(f"文件: {file_path}")

        def do_replace():
            try:
                with open(file_path, 'rb') as f:
                    files = {'file': f}
                    url = f"{MANAGER_BASE_URL}/workstations/{ws_id}/replace_model"
                    response = requests.post(url, files=files, timeout=60)
                    response.raise_for_status()
                    data = response.json()

                if data.get("ok"):
                    message = data.get("message", "模型替换成功")
                    self.log(f"✓ 工位 {ws_id}: {message}")
                    self.update_status("模型替换完成", "green")
                    self.show_info(
                        "操作成功",
                        f"工位 {ws_id} 模型替换成功\n模型已更新并已重启服务"
                    )
                else:
                    error = data.get("error", "未知错误")
                    self.log(f"✗ 工位 {ws_id}: {error}")
                    self.update_status("模型替换失败", "red")
                    self.show_error(
                        "错误",
                        f"工位 {ws_id} 模型替换失败:\n{error}"
                    )
            except Exception as e:
                self.log(f"✗ 模型替换失败: {e}")
                self.update_status("模型替换失败", "red")
                self.show_error("错误", f"模型替换失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_replace, daemon=True)
        thread.start()

    def check_system_services_on_startup(self):
        """启动时检查系统服务"""
        def do_check():
            self.log("\n检查系统服务状态...")
            self.check_frpc_service()

        thread = threading.Thread(target=do_check, daemon=True)
        thread.start()

    def update_netplan_status_display(self, text, color="gray"):
        if hasattr(self, "netplan_status_label"):
            self.netplan_status_label.config(text=f"状态: {text}", fg=color)

    def update_netplan_list_display(self, interfaces, netplan_file=""):
        """更新网口列表显示（展示 iface + IP + Gateway，可修改并 apply）"""
        # 按数量自适应高度（含文件行的额外高度）
        extra = 28 if netplan_file else 0
        count = len(interfaces or [])
        if count <= 0:
            h = 90
        else:
            h = extra + count * 60  # 每个网口占用更多高度（增加了网关行和分隔线）
            # 设置最小和最大高度，确保有足够空间显示内容
            h = max(100, min(300, h))
        try:
            if hasattr(self, "netplan_canvas"):
                self.netplan_canvas.configure(height=h)
        except Exception:
            pass

        # 清空
        if not hasattr(self, "netplan_list_frame"):
            return
        for widget in self.netplan_list_frame.winfo_children():
            widget.destroy()
        self.netplan_rows.clear()

        for it in interfaces:
            iface = it.get("name") or ""
            # 只显示 IP（不显示 /24）
            addr = it.get("ip") or it.get("address") or ""
            gateway = it.get("gateway") or ""
            dhcp4 = it.get("dhcp4")

            # 网口容器（包含两行：IP行和网关行）
            iface_container = tk.Frame(self.netplan_list_frame, pady=4)
            iface_container.pack(fill=tk.X, padx=5)

            # 第一行：网口名称 + DHCP状态 + IP
            row1 = tk.Frame(iface_container)
            row1.pack(fill=tk.X)

            name_label = tk.Label(
                row1,
                text=iface,
                font=("Arial", 10, "bold"),
                width=12,
                anchor="w"
            )
            name_label.pack(side=tk.LEFT, padx=5)

            dhcp_text = ""
            if dhcp4 is True:
                dhcp_text = "DHCP4: true"
            elif dhcp4 is False:
                dhcp_text = "DHCP4: false"
            dhcp_label = tk.Label(
                row1,
                text=dhcp_text,
                font=("Arial", 9),
                fg="gray",
                width=12,
                anchor="w"
            )
            dhcp_label.pack(side=tk.LEFT, padx=5)

            tk.Label(
                row1,
                text="IP:",
                font=("Arial", 9),
                width=6,
                anchor="e"
            ).pack(side=tk.LEFT)

            ip_entry = tk.Entry(row1, width=18, font=("Arial", 10))
            ip_entry.insert(0, addr)
            ip_entry.pack(side=tk.LEFT, padx=5)

            # 第二行：网关
            row2 = tk.Frame(iface_container)
            row2.pack(fill=tk.X, pady=(2, 0))

            # 空白占位（对齐网口名称和DHCP状态）
            tk.Label(row2, text="", width=12).pack(side=tk.LEFT, padx=5)
            tk.Label(row2, text="", width=12).pack(side=tk.LEFT, padx=5)

            tk.Label(
                row2,
                text="网关:",
                font=("Arial", 9),
                width=6,
                anchor="e"
            ).pack(side=tk.LEFT)

            gateway_entry = tk.Entry(row2, width=18, font=("Arial", 10))
            gateway_entry.insert(0, gateway)
            gateway_entry.pack(side=tk.LEFT, padx=5)

            apply_btn = tk.Button(
                row2,
                text="Apply",
                command=lambda ifn=iface, ip_ent=ip_entry, gw_ent=gateway_entry: self.apply_netplan_address(ifn, ip_ent.get(), gw_ent.get()),
                bg="#4CAF50",
                fg="white",
                font=("Arial", 9),
                width=8,
                cursor="hand2"
            )
            apply_btn.pack(side=tk.LEFT, padx=5)

            # 分隔线
            separator = tk.Frame(iface_container, height=1, bg="lightgray")
            separator.pack(fill=tk.X, pady=3)

            self.netplan_rows[iface] = {"ip_entry": ip_entry, "gateway_entry": gateway_entry, "btn": apply_btn}

    def load_netplan_interfaces(self):
        """加载 netplan 网口配置"""
        if self.is_processing:
            return

        def do_load():
            try:
                self.update_netplan_status_display("加载中...", "gray")
                data = self.call_manager_api("netplan/interfaces", method="GET")
                if data.get("ok"):
                    interfaces = data.get("interfaces", [])
                    netplan_file = data.get("netplan_file", "")
                    self.update_netplan_list_display(interfaces, netplan_file=netplan_file)
                    self.update_netplan_status_display(f"已加载 {len(interfaces)} 个网口", "green")
                    self.log(f"✓ 网口配置已加载: {netplan_file}")
                else:
                    err = data.get("error", "未知错误")
                    self.update_netplan_list_display([], netplan_file="")
                    self.update_netplan_status_display("加载失败", "red")
                    self.log(f"✗ 加载网口配置失败: {err}")
            except Exception as e:
                self.update_netplan_list_display([], netplan_file="")
                self.update_netplan_status_display("加载异常", "red")
                self.log(f"✗ 加载网口配置异常: {e}")

        threading.Thread(target=do_load, daemon=True).start()

    def apply_netplan_address(self, iface: str, address: str, gateway: str = ""):
        """设置指定网口 address 和 gateway 并调用 netplan apply"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        address = (address or "").strip()
        gateway = (gateway or "").strip()
        
        if not address:
            messagebox.showwarning("提示", "请输入 IP 地址，例如 192.169.1.10")
            return

        # 简单校验：IPv4（只校验 IP，不管 /24）
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", address):
            messagebox.showwarning("提示", "IP地址格式不正确，请输入 IP，例如 192.169.1.10")
            return

        # 校验网关（如果提供）
        if gateway and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", gateway):
            messagebox.showwarning("提示", "网关地址格式不正确，请输入 IP，例如 192.169.1.1")
            return

        # 确认消息
        confirm_msg = f"确定要将 {iface} 设置为以下配置并应用(netplan apply)吗？\n\n"
        confirm_msg += f" IP: {address}\n"
        if gateway:
            confirm_msg += f"网关: {gateway}"
        else:
            confirm_msg += "网关: (不修改)"
        
        if not messagebox.askyesno("确认", confirm_msg):
            return

        self.set_buttons_state(False)
        self.update_status(f"正在设置 {iface} ...", "blue")
        log_msg = f"\n设置网口 {iface}\n  IP -> {address}"
        if gateway:
            log_msg += f"\n  网关 -> {gateway}"
        self.log(log_msg)

        def do_apply():
            try:
                payload = {"ip": address, "apply": True}
                if gateway:
                    payload["gateway"] = gateway
                    
                data = self.call_manager_api_json(f"netplan/interfaces/{iface}/address", payload, timeout=60)
                if data.get("ok"):
                    self.log(f"✓ {iface} 已设置并应用成功")
                    apply_info = data.get("apply", {})
                    if apply_info.get("stdout"):
                        self.log(f"stdout:\n{apply_info.get('stdout')}")
                    if apply_info.get("stderr"):
                        self.log(f"stderr:\n{apply_info.get('stderr')}")
                    self.update_status("网口设置完成", "green")
                    # 刷新显示
                    self.load_netplan_interfaces()
                    success_msg = f"{iface} 配置已设置并应用成功\n\nIP: {address}"
                    if gateway:
                        success_msg += f"\n网关: {gateway}"
                    messagebox.showinfo("成功", success_msg)
                else:
                    err = data.get("error") or data.get("apply", {}).get("error") or "未知错误"
                    self.log(f"✗ {iface} 设置失败: {err}")
                    self.update_status("网口设置失败", "red")
                    messagebox.showerror("错误", f"{iface} 设置失败:\n{err}")
            except Exception as e:
                self.log(f"✗ 网口设置异常: {e}")
                self.update_status("网口设置失败", "red")
                messagebox.showerror("错误", f"网口设置异常:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        threading.Thread(target=do_apply, daemon=True).start()

    def load_frp_status(self):
        """检查 frpc.ini 是否为模板，决定是否显示/启用初始化"""
        if not hasattr(self, "frp_init_status_label"):
            return

        def do_check():
            try:
                self.frp_init_status_label.config(text="检查中...", fg="gray")
                data = self.call_manager_api("frp/status", method="GET")
                if data.get("ok"):
                    needs_init = data.get("needs_init", False)
                    initialized = data.get("initialized", False)
                    current_port = data.get("current_port")
                    ssh_section = data.get("ssh_section")

                    if needs_init:
                        self.frp_init_status_label.config(text="需要初始化（请输入端口）", fg="orange")
                        self.frp_port_entry.config(state=tk.NORMAL)
                        self.frp_init_btn.config(state=tk.NORMAL)
                    elif initialized:
                        txt = f"已初始化: {ssh_section} / {current_port}" if current_port else f"已初始化: {ssh_section}"
                        self.frp_init_status_label.config(text=txt, fg="green")
                        self.frp_port_entry.delete(0, tk.END)
                        if current_port:
                            self.frp_port_entry.insert(0, str(current_port))
                        self.frp_port_entry.config(state=tk.DISABLED)
                        self.frp_init_btn.config(state=tk.DISABLED)
                    else:
                        # 非模板且未识别为已初始化
                        txt = f"当前配置不可初始化（段: {ssh_section}）" if ssh_section else "当前配置不可初始化"
                        self.frp_init_status_label.config(text=txt, fg="gray")
                        self.frp_port_entry.config(state=tk.DISABLED)
                        self.frp_init_btn.config(state=tk.DISABLED)
                else:
                    err = data.get("error", "未知错误")
                    self.frp_init_status_label.config(text=f"检查失败: {err}", fg="red")
                    self.frp_port_entry.config(state=tk.DISABLED)
                    self.frp_init_btn.config(state=tk.DISABLED)
            except Exception as e:
                self.frp_init_status_label.config(text=f"检查失败: {e}", fg="red")
                try:
                    self.frp_port_entry.config(state=tk.DISABLED)
                    self.frp_init_btn.config(state=tk.DISABLED)
                except Exception:
                    pass

        threading.Thread(target=do_check, daemon=True).start()

    def initialize_frp(self):
        """初始化 frpc.ini（只允许一次）并 enable+restart frpc.service"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        port_str = (self.frp_port_entry.get() or "").strip()
        if not port_str.isdigit():
            messagebox.showwarning("提示", "请输入数字端口，例如 6305")
            return
        port = int(port_str)
        if port <= 0 or port > 65535:
            messagebox.showwarning("提示", "端口范围必须是 1-65535")
            return

        if not messagebox.askyesno("确认", f"确定初始化 FRP 端口为 {port}？\n初始化后将无法修改。"):
            return

        self.set_buttons_state(False)
        self.update_status("正在初始化 FRP...", "blue")
        self.log(f"\n初始化 FRP 端口: {port}")

        def do_init():
            try:
                data = self.call_manager_api_json("frp/initialize", {"port": port}, timeout=60)
                if data.get("ok"):
                    self.log("✓ FRP 初始化成功，已设置开机自启并重启 frpc.service")
                    self.update_status("FRP 初始化完成", "green")
                    # 刷新状态并锁定
                    self.load_frp_status()
                    messagebox.showinfo("成功", f"FRP 初始化成功，端口: {port}")
                else:
                    err = data.get("error", "未知错误")
                    self.log(f"✗ FRP 初始化失败: {err}")
                    self.update_status("FRP 初始化失败", "red")
                    messagebox.showerror("错误", f"FRP 初始化失败:\n{err}")
            except Exception as e:
                self.log(f"✗ FRP 初始化异常: {e}")
                self.update_status("FRP 初始化失败", "red")
                messagebox.showerror("错误", f"FRP 初始化异常:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        threading.Thread(target=do_init, daemon=True).start()

    def _set_list_canvas_height(self, canvas_attr: str, count: int, row_height: int, min_h: int, max_h: int, extra: int = 0):
        """根据数量自动调整列表 Canvas 高度（限制最大高度，避免页面过长）。"""
        canvas = getattr(self, canvas_attr, None)
        if canvas is None:
            return
        if count <= 0:
            h = min_h
        else:
            h = extra + count * row_height
            h = max(min_h, min(max_h, h))
        try:
            canvas.configure(height=h)
        except Exception:
            pass

    def update_lights_list_display(self):
        """更新三色灯列表显示"""
        # 按数量自适应高度
        self._set_list_canvas_height("lights_canvas", len(self.lights_list or []), row_height=36, min_h=60, max_h=150, extra=10)

        # 清空现有列表
        for widget in self.lights_list_frame.winfo_children():
            widget.destroy()
        self.light_test_buttons.clear()
        
        if not self.lights_list:
            no_lights_label = tk.Label(
                self.lights_list_frame,
                text="未找到三色灯设备",
                font=("Arial", 10),
                fg="gray"
            )
            no_lights_label.pack(pady=20)
            return
        
        # 显示每个三色灯
        for light in self.lights_list:
            light_id = light.get("light_id")
            device_path = light.get("device_path")
            connected = light.get("connected")
            
            # 创建每个灯的行
            light_row = tk.Frame(self.lights_list_frame, pady=5)
            light_row.pack(fill=tk.X, padx=5)
            
            # 状态指示器
            status_color = "green" if connected else "red"
            status_canvas = tk.Canvas(light_row, width=10, height=10)
            status_canvas.create_oval(2, 2, 10, 10, fill=status_color, outline="")
            status_canvas.pack(side=tk.LEFT, padx=5)
            
            # 灯的名称
            name_label = tk.Label(
                light_row,
                text=light_id,
                font=("Arial", 10, "bold"),
                width=15,
                anchor="w"
            )
            name_label.pack(side=tk.LEFT, padx=5)
            
            # 设备路径
            path_label = tk.Label(
                light_row,
                text=f"({device_path})",
                font=("Arial", 9),
                fg="gray",
                anchor="w"
            )
            path_label.pack(side=tk.LEFT, padx=5)
            
            # 测试按钮
            test_btn = tk.Button(
                light_row,
                text="测试",
                command=lambda lid=light_id: self.test_single_light(lid),
                bg="#9C27B0",
                fg="white",
                font=("Arial", 9),
                width=8,
                cursor="hand2",
                state=tk.NORMAL if connected else tk.DISABLED
            )
            test_btn.pack(side=tk.RIGHT, padx=5)
            self.light_test_buttons[light_id] = test_btn
    
    def update_lights_status_display(self, text, color="gray"):
        """更新三色灯状态显示"""
        self.lights_status_label.config(text=f"状态: {text}", fg=color)
    
    def check_lights_on_startup(self):
        """启动时检查三色灯状态"""
        def do_check():
            self.log("\n检查三色灯状态...")
            data = self.call_manager_api("lights/status", method="GET")
            
            if data.get("ok"):
                lights = data.get("lights", {})
                count = data.get("count", 0)
                
                if count == 0:
                    self.log("⚠ 未找到任何三色灯设备 (/dev/ttyUSB_light_*)")
                    self.log("正在尝试自动创建符号链接...")
                    # 尝试自动初始化，会自动创建符号链接
                    self.auto_initialize_lights()
                    return
                
                # 检查是否所有灯都已初始化
                all_initialized = all(
                    light.get("initialized") and light.get("connected")
                    for light in lights.values()
                )
                
                if all_initialized:
                    self.log(f"✓ 检测到 {count} 个三色灯，全部已初始化")
                    self.update_lights_status_display(f"{count} 个设备已就绪", "green")
                    # 加载三色灯列表
                    self.load_lights_list()
                else:
                    self.log(f"⚠ 检测到 {count} 个三色灯，部分未初始化")
                    self.log("正在自动初始化...")
                    self.auto_initialize_lights()
            else:
                error = data.get("error", "未知错误")
                self.log(f"✗ 检查三色灯状态失败: {error}")
                if "未找到任何三色灯设备" in error:
                    self.log("正在尝试自动配置...")
                    self.auto_initialize_lights()
                else:
                    self.update_lights_status_display("检查失败", "red")
        
        thread = threading.Thread(target=do_check, daemon=True)
        thread.start()
    
    def auto_initialize_lights(self):
        """自动初始化三色灯（不弹窗）"""
        # 添加 auto_setup 参数，自动配置 udev
        data = self.call_manager_api("lights/initialize", params={"force": "true", "auto_setup": "true"})
        
        if data.get("ok"):
            results = data.get("results", {})
            success_count = sum(1 for r in results.values() if r.get("success"))
            total_count = len(results)
            
            self.log(f"✓ 三色灯初始化完成: {success_count}/{total_count} 成功")
            
            for name, result in results.items():
                status = result.get("status", "unknown")
                self.log(f"  - {name}: {status}")
            
            # 刷新列表
            self.load_lights_list()
            self.update_lights_status_display(
                f"{success_count}/{total_count} 个设备已就绪",
                "green" if success_count == total_count else "orange"
            )
        else:
            error = data.get("error", "未知错误")
            self.log(f"✗ 初始化失败: {error}")
            
            # 如果是设备未找到，给出操作提示
            if "未找到任何三色灯设备" in error or "ttyUSB" in error:
                self.log("")
                self.log("可能的解决方法:")
                self.log("1. 检查 USB 设备是否已连接")
                self.log("2. 重新插入 USB 三色灯设备")
                self.log("3. 点击'初始化'按钮重新扫描")
                self.log("4. 或者重启服务")
                self.log("")
                self.log("说明: 符号链接基于 USB 物理端口")
                self.log("     相同的 USB 口 → 相同的设备名称")
            
            self.update_lights_status_display("初始化失败", "red")
    
    def load_lights_list(self):
        """加载三色灯列表"""
        data = self.call_manager_api("lights/list", method="GET")
        
        if data.get("ok"):
            self.lights_list = data.get("lights", [])
            count = data.get("count", 0)
            self.log(f"加载了 {count} 个三色灯")
            self.update_lights_list_display()
        else:
            error = data.get("error", "未知错误")
            self.log(f"加载三色灯列表失败: {error}")
            self.lights_list = []
            self.update_lights_list_display()
    
    def refresh_lights_list(self):
        """刷新三色灯列表"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return
        
        self.set_buttons_state(False)
        self.log("\n刷新三色灯列表...")
        
        def do_refresh():
            try:
                self.load_lights_list()
            finally:
                self.set_buttons_state(True)
        
        thread = threading.Thread(target=do_refresh, daemon=True)
        thread.start()
    
    def initialize_lights(self):
        """初始化三色灯"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return
        
        if not messagebox.askyesno("确认", "确定要初始化所有三色灯吗？\n这将重新连接所有设备。"):
            return
        
        self.set_buttons_state(False)
        self.update_status("正在初始化三色灯...", "blue")
        self.log("\n" + "="*50)
        self.log("开始初始化三色灯...")
        
        def do_initialize():
            try:
                # 添加 auto_setup 参数，自动配置 udev
                data = self.call_manager_api("lights/initialize", params={"force": "true", "auto_setup": "true"})
                
                if data.get("ok"):
                    results = data.get("results", {})
                    success_count = sum(1 for r in results.values() if r.get("success"))
                    total_count = len(results)
                    
                    self.log(f"初始化完成: {success_count}/{total_count} 成功")
                    
                    for name, result in results.items():
                        status = result.get("status", "unknown")
                        success = result.get("success", False)
                        icon = "✓" if success else "✗"
                        self.log(f"  {icon} {name}: {status}")
                    
                    self.log("="*50)
                    self.update_status("初始化完成", "green")
                    
                    # 刷新列表
                    self.load_lights_list()
                    self.update_lights_status_display(
                        f"{success_count}/{total_count} 个设备已就绪",
                        "green" if success_count == total_count else "orange"
                    )
                    
                    self.show_info("成功", f"三色灯初始化完成\n成功: {success_count}/{total_count}")
                else:
                    error = data.get("error", "未知错误")
                    self.log(f"初始化失败: {error}")
                    self.log("="*50)
                    self.update_status("初始化失败", "red")
                    self.show_error("错误", f"初始化失败:\n{error}")
            except Exception as e:
                self.log(f"初始化失败: {e}")
                self.log("="*50)
                self.update_status("初始化失败", "red")
                self.show_error("错误", f"初始化失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)
        
        thread = threading.Thread(target=do_initialize, daemon=True)
        thread.start()
    
    def test_single_light(self, light_id):
        """测试单个三色灯"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return
        
        self.set_buttons_state(False)
        self.update_status(f"正在测试 {light_id}...", "blue")
        self.log(f"\n开始测试 {light_id}...")
        self.log("将依次点亮: 绿 -> 黄 -> 红")
        
        def do_test():
            try:
                data = self.call_manager_api("lights/test", params={"light_id": light_id})
                
                if data.get("ok"):
                    message = data.get("message", "测试成功")
                    self.log(f"✓ {light_id}: {message}")
                    self.update_status("测试完成", "green")
                else:
                    error = data.get("error", data.get("message", "未知错误"))
                    self.log(f"✗ {light_id}: {error}")
                    self.update_status("测试失败", "red")
                    self.show_error("测试失败", f"{light_id} 测试失败:\n{error}")
            except Exception as e:
                self.log(f"✗ 测试失败: {e}")
                self.update_status("测试失败", "red")
                self.show_error("错误", f"测试失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)
        
        thread = threading.Thread(target=do_test, daemon=True)
        thread.start()

    def check_frpc_service(self):
        """检查 FRPC 服务状态"""
        def do_check():
            try:
                self.frpc_status_label.config(text="检查中...", fg="gray")
                data = self.call_manager_api("system/check_frpc", method="GET")

                if data.get("ok"):
                    active = data.get("active", False)
                    proxy_success = data.get("proxy_success", False)

                    if active and proxy_success:
                        proxy_name = data.get("proxy_name", "")
                        status_text = f"✓ 运行中 (代理: {proxy_name})" if proxy_name else "✓ 运行中"
                        self.frpc_status_label.config(text=status_text, fg="green")
                        self.log(f"✓ FRPC 服务运行正常，代理启动成功")
                    elif active:
                        self.frpc_status_label.config(text="⚠ 运行中但代理未启动", fg="orange")
                        self.log(f"⚠ FRPC 服务运行中，但代理未启动成功")
                    else:
                        self.frpc_status_label.config(text="✗ 未运行", fg="red")
                        self.log(f"✗ FRPC 服务未运行")
                else:
                    error = data.get("error", "未知错误")
                    self.frpc_status_label.config(text=f"检查失败: {error}", fg="red")
                    self.log(f"✗ 检查 FRPC 服务失败: {error}")
            except Exception as e:
                self.frpc_status_label.config(text=f"检查失败: {str(e)}", fg="red")
                self.log(f"✗ 检查 FRPC 服务异常: {e}")

        thread = threading.Thread(target=do_check, daemon=True)
        thread.start()

    def activate_workstation(self, ws_id: str):
        """激活工位"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return

        # 创建密码输入对话框
        password_dialog = tk.Toplevel(self.root)
        password_dialog.title(f"激活工位 {ws_id}")
        password_dialog.geometry("400x150")
        password_dialog.resizable(False, False)
        
        # 居中显示
        password_dialog.update_idletasks()
        width = password_dialog.winfo_width()
        height = password_dialog.winfo_height()
        x = (password_dialog.winfo_screenwidth() // 2) - (width // 2)
        y = (password_dialog.winfo_screenheight() // 2) - (height // 2)
        password_dialog.geometry(f'{width}x{height}+{x}+{y}')
        
        # 提示文本
        tk.Label(
            password_dialog,
            text=f"请输入 ccai 用户的密码以激活工位 {ws_id}:",
            font=("Arial", 10),
            pady=10
        ).pack()
        
        # 密码输入框
        password_var = tk.StringVar()
        password_entry = tk.Entry(
            password_dialog,
            textvariable=password_var,
            show="*",
            font=("Arial", 11),
            width=30
        )
        password_entry.pack(pady=10)
        password_entry.focus()
        
        # 按钮框架
        btn_frame = tk.Frame(password_dialog)
        btn_frame.pack(pady=10)
        
        result = {"confirmed": False}
        
        def on_confirm():
            result["confirmed"] = True
            result["password"] = password_var.get()
            password_dialog.destroy()
        
        def on_cancel():
            result["confirmed"] = False
            password_dialog.destroy()
        
        # 确认按钮
        tk.Button(
            btn_frame,
            text="确认",
            command=on_confirm,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 10),
            width=10,
            cursor="hand2"
        ).pack(side=tk.LEFT, padx=5)
        
        # 取消按钮
        tk.Button(
            btn_frame,
            text="取消",
            command=on_cancel,
            bg="#f44336",
            fg="white",
            font=("Arial", 10),
            width=10,
            cursor="hand2"
        ).pack(side=tk.LEFT, padx=5)
        
        # 绑定回车键
        password_entry.bind("<Return>", lambda e: on_confirm())
        
        # 等待对话框关闭
        self.root.wait_window(password_dialog)
        
        if not result.get("confirmed"):
            self.log("用户取消激活操作")
            return
        
        password = result.get("password", "")
        if not password:
            messagebox.showwarning("警告", "密码不能为空")
            return
        
        self.set_buttons_state(False)
        self.update_status(f"正在激活工位 {ws_id}...", "blue")
        self.log(f"\n激活工位 {ws_id}...")

        def do_activate():
            try:
                url = f"{MANAGER_BASE_URL}/workstations/{ws_id}/activate"
                response = requests.post(
                    url,
                    json={"password": password},
                    timeout=60
                )
                response.raise_for_status()
                data = response.json()

                if data.get("ok"):
                    message = data.get("message", "激活成功")
                    self.log(f"✓ 工位 {ws_id}: {message}")
                    self.update_status("激活完成", "green")
                    self.show_info("操作成功", f"工位 {ws_id} 激活成功")
                    
                    # 刷新工位列表
                    self.log("\n刷新工位列表...")
                    self.load_workstations_list()
                else:
                    error = data.get("error", "未知错误")
                    self.log(f"✗ 工位 {ws_id}: {error}")
                    self.update_status("激活失败", "red")
                    self.show_error("错误", f"工位 {ws_id} 激活失败:\n{error}")
            except Exception as e:
                self.log(f"✗ 激活失败: {e}")
                self.update_status("激活失败", "red")
                self.show_error("错误", f"激活失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_activate, daemon=True)
        thread.start()

    def delete_workstation(self, ws_id: str):
        """删除工位"""
        if self.is_processing:
            messagebox.showwarning("警告", "操作正在进行中，请稍候...")
            return
        
        # 检查工位是否已激活
        ws_info = None
        for ws in self.workstations_list:
            if ws.get("id") == ws_id:
                ws_info = ws
                break
        
        warning_msg = f"确定要删除工位 {ws_id} 吗？\n\n"
        warning_msg += "⚠️ 警告：此操作将永久删除工位目录及其所有文件，\n"
        warning_msg += "且无法恢复！\n\n"
        
        if ws_info and ws_info.get("is_activated"):
            warning_msg += "⚠️ 注意：该工位已激活，删除前请确保已停止服务！\n\n"
        
        warning_msg += "📌 说明：只能删除最大ID的工位，0号工位无法删除。"

        if not messagebox.askyesno("确认删除", warning_msg):
            return

        self.set_buttons_state(False)
        self.update_status(f"正在删除工位 {ws_id}...", "blue")
        self.log(f"\n删除工位 {ws_id}...")

        def do_delete():
            try:
                data = self.call_manager_api(f"workstations/{ws_id}/delete")

                if data.get("ok"):
                    message = data.get("message", "删除成功")
                    self.log(f"✓ 工位 {ws_id}: {message}")
                    self.update_status("删除完成", "green")
                    self.show_info("操作成功", f"工位 {ws_id} 删除成功")
                    
                    # 刷新工位列表
                    self.log("\n刷新工位列表...")
                    self.load_workstations_list()
                else:
                    error = data.get("error", "未知错误")
                    self.log(f"✗ 工位 {ws_id}: {error}")
                    self.update_status("删除失败", "red")
                    self.show_error("错误", f"工位 {ws_id} 删除失败:\n{error}")
            except Exception as e:
                self.log(f"✗ 删除失败: {e}")
                self.update_status("删除失败", "red")
                self.show_error("错误", f"删除失败:\n{str(e)}")
            finally:
                self.set_buttons_state(True)

        thread = threading.Thread(target=do_delete, daemon=True)
        thread.start()


def main():
    root = tk.Tk()
    app = ServiceControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
