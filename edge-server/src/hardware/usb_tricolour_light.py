import serial
import time


OPS_MAP = {
    "green": {
        0: "FF010101AA",
        1: "FF020101AA",
    },
    "yellow": {
        0: "FF010101AA",
        1: "FF030101AA",
    },
    "red": {
        0: "FF010101AA",
        1: "FF040101AA",
    },
    "red,buzzer": {
        0: "FF010101AA",
        1: "FF040201AA",
    },
}

def init_light():
    try:
        serialPort = "/dev/ttyUSB0"
        baudRate = 9600  # 波特率
        s = serial.Serial(serialPort, baudRate, timeout=0.5)
        return s
    except Exception as e:
        print(e)
        return None

def set_light(s, ops_index):
    handle_msg(s, ops_index, 1)

def off_light(s):
    handle_msg(s, 0, 0)
    handle_msg(s, 1, 0)
    handle_msg(s, 2, 0)
    handle_msg(s, 3, 0)


# 绿 黄 红 蜂鸣器
def handle_msg(s, ops_index, ops_value):
    if s is None:
        return False
    def _close_all_light():
        # 关闭所有灯光：绿、黄、红
        bin_data = bytes.fromhex(OPS_MAP.get("green").get(0))
        s.write(bin_data)
        time.sleep(0.05)
        bin_data = bytes.fromhex(OPS_MAP.get("yellow").get(0))
        s.write(bin_data)
        time.sleep(0.05)
        bin_data = bytes.fromhex(OPS_MAP.get("red").get(0))
        s.write(bin_data)
        time.sleep(0.05)

    try:
        _close_all_light()
        if ops_value == 0:
            return True
        # 将ops_index转换为颜色字符串
        color_map = {0: "green", 1: "yellow", 2: "red", 3: "red,buzzer"}
        color_string = color_map.get(ops_index)
        if color_string is None:
            return False
        ops_hex = OPS_MAP.get(color_string, {}).get(ops_value)
        if ops_hex is None:
            return False
        bin_data = bytes.fromhex(ops_hex)
        s.write(bin_data)
        return True
    except Exception as e:
        print(e)
        return False


if __name__ == '__main__':
    s = init_light()
    handle_msg(s, 3, 1)
    time.sleep(1)
    handle_msg(s, 2, 1)
    time.sleep(1)
    handle_msg(s, 1, 1)
    time.sleep(1)
    handle_msg(s, 0, 1)
    time.sleep(1)
