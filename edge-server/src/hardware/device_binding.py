import hashlib
import os
import json
import psutil

def load_device_config():
    try:
        config_path = 'device.json'
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
                return config.get('AUTHORIZED_DEVICE_ID')
        return None
    except Exception as e:
        print(f"读取设备配置文件失败: {str(e)}")
        return None

def load_all_authorized_device_ids():
    """加载所有可能的授权设备ID"""
    try:
        config_path = 'device.json'
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
                # 优先使用新的简化格式
                all_device_ids = config.get('all_possible_device_ids', [])
                if all_device_ids:
                    return all_device_ids
                # 如果没有all_possible_device_ids，则使用旧的AUTHORIZED_DEVICE_ID
                authorized_id = config.get('AUTHORIZED_DEVICE_ID')
                return [authorized_id] if authorized_id else []
        return []
    except Exception as e:
        print(f"读取设备配置文件失败: {str(e)}")
        return []

AUTHORIZED_DEVICE_ID = load_device_config()
ALL_AUTHORIZED_DEVICE_IDS = load_all_authorized_device_ids()

# 获取WiFi网口的 MAC 地址
def get_network_interfaces_with_prefix():
    network_info = psutil.net_if_addrs()
    mac_addresses = []

    for interface, addrs in network_info.items():
        # 只获取以 wl 开头的WiFi网口，排除回环接口和虚拟网口
        if (interface.startswith("wl") and 
            not interface.startswith("lo") and 
            not interface.startswith("docker") and
            not interface.startswith("veth")):
            for addr in addrs:
                if addr.family == psutil.AF_LINK:  # 只获取 MAC 地址
                    mac_addresses.append(addr.address)
    
    return mac_addresses

# 获取设备 ID
def get_device_id():
    mac_addresses = get_network_interfaces_with_prefix()  # 获取WiFi网口的 MAC 地址

    # 确保至少有一个MAC地址，不足的用空字符串补充
    if len(mac_addresses) == 0:
        mac_addresses.append("")
        
    mac_address = mac_addresses[0]
    
    device_id = hashlib.sha256(mac_address.encode()).hexdigest()
    
    return device_id, mac_address

# 获取设备 ID（WiFi网卡只有一个MAC地址）
def get_all_possible_device_ids():
    mac_addresses = get_network_interfaces_with_prefix()  # 获取WiFi网口的 MAC 地址

    # 确保至少有一个MAC地址，不足的用空字符串补充
    if len(mac_addresses) == 0:
        mac_addresses.append("")
        
    mac_address = mac_addresses[0]
    
    # 只有一个MAC地址，直接生成设备ID
    device_id = hashlib.sha256(mac_address.encode()).hexdigest()
    
    return [(device_id, mac_address)]

def verify_device():
    try:
        if not ALL_AUTHORIZED_DEVICE_IDS:
            print("未找到设备配置文件 device.json 或配置无效")
            return False
            
        # 获取当前设备的ID
        possible_device_ids = get_all_possible_device_ids()
        
        # 检查是否有任何一个设备ID匹配
        is_authorized = False
        matched_device_id = None
        matched_mac = None
        
        print(f"当前检测到的WiFi MAC地址:")
        for i, (device_id, mac) in enumerate(possible_device_ids, 1):
            print(f"  MAC{i}: {mac} -> 设备ID: {device_id}")
            
        print(f"\n授权设备ID列表:")
        for i, auth_id in enumerate(ALL_AUTHORIZED_DEVICE_IDS, 1):
            print(f"  授权ID{i}: {auth_id}")
        
        # 检查当前设备ID是否在授权列表中
        for device_id, mac in possible_device_ids:
            if device_id in ALL_AUTHORIZED_DEVICE_IDS:
                is_authorized = True
                matched_device_id = device_id
                matched_mac = mac
                break
        
        if is_authorized:
            print(f"\n设备验证通过")
            print(f"匹配的设备ID: {matched_device_id}")
            print(f"匹配的WiFi MAC地址: {matched_mac}")
        else:
            print(f"\n设备验证失败")
            print(f"当前设备ID与所有授权设备ID都不匹配")
        
        return is_authorized
        
    except Exception as e:
        raise Exception(f"设备验证失败: {str(e)}")

def get_current_device_id():
    try:
        device_id, mac = get_device_id()
        print(f"当前设备ID: {device_id}")
        print(f"WiFi MAC地址: {mac}")
        return device_id
    except Exception as e:
        print(f"获取设备ID失败: {str(e)}")
        return None 
