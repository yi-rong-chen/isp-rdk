import subprocess
import hashlib
import json
import glob
import os
from datetime import datetime
import src.core.config.global_var as g
from src.hardware.device_binding import get_device_id

def get_mac(path):
    try:
        return subprocess.check_output(f"cat {path}", shell=True).decode().strip()
    except Exception:
        return ""

if __name__ == '__main__':
    # 获取WiFi网卡的设备ID
    device_id, mac = get_device_id()
    
    print(f"检测到的WiFi MAC地址: {mac}")
    print(f"生成的设备ID: {device_id}")
    
    # 生成简化的配置文件，只保存一个设备ID
    device_config = {
        "AUTHORIZED_DEVICE_ID": device_id,
        "all_possible_device_ids": [device_id],
        "wifi_mac": mac,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    with open("device.json", "w") as f:
        json.dump(device_config, f, indent=4)
    print("设备配置文件已生成: device.json")
    print("注意: 使用WiFi网卡MAC地址进行设备验证")