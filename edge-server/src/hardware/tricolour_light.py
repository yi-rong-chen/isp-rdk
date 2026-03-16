"""
三色灯统一接口
仅支持USB实现
"""
from abc import ABC, abstractmethod
import src.core.config.global_var as g
from src.hardware.usb_tricolour_light import init_light, handle_msg


class TricolourLight(ABC):
    """三色灯抽象基类"""
    
    @abstractmethod
    def set_light(self, color_string):
        """
        设置灯光颜色
        Args:
            color_string: 颜色字符串，如 "yellow", "green", "red", "red,buzzer"
        """
        pass
    
    @abstractmethod
    def off_light(self):
        """关闭所有灯光"""
        pass
    
    @abstractmethod
    def is_connected(self):
        """检查连接状态"""
        pass
    
    @abstractmethod
    def exit(self):
        """清理资源"""
        pass


class UsbTricolourLight(TricolourLight):
    """基于USB串口的三色灯实现"""
    
    # USB三色灯的颜色映射到ops_index
    COLOR_TO_OPS_INDEX = {
        "green": 0,
        "yellow": 1,
        "red": 2,
        "red,buzzer": 3
    }
    
    def __init__(self):
        """初始化USB三色灯"""
        self.serial = init_light()
        if self.serial is None:
            g.logger.warning("USB三色灯初始化失败")
    
    def set_light(self, color_string):
        """设置灯光颜色"""
        if self.serial is None:
            g.logger.warning(f"USB三色灯未连接，跳过设置灯光: {color_string}")
            return
        
        # 将颜色字符串转换为ops_index
        ops_index = self.COLOR_TO_OPS_INDEX.get(color_string)
        if ops_index is None:
            g.logger.error(f"不支持的颜色: {color_string}")
            return
        
        handle_msg(self.serial, ops_index, 1)
    
    def off_light(self):
        """关闭所有灯光"""
        if self.serial is None:
            g.logger.warning("USB三色灯未连接，跳过关闭灯光")
            return
        
        # 关闭所有灯光（绿、黄、红、蜂鸣器）
        for ops_index in range(4):
            handle_msg(self.serial, ops_index, 0)
    
    def is_connected(self):
        """检查连接状态"""
        return self.serial is not None
    
    def exit(self):
        """清理资源"""
        if self.serial is not None:
            try:
                self.serial.close()
                g.logger.info("USB三色灯连接已关闭")
            except Exception as e:
                g.logger.error(f"关闭USB三色灯连接时出错: {e}")


def create_tricolour_light(light_type="usb", **kwargs):
    """
    工厂函数：创建USB三色灯实例
    Args:
        light_type: 历史兼容参数，当前仅支持 "usb"
        **kwargs: 历史兼容参数，当前未使用
    Returns:
        TricolourLight实例，如果初始化失败返回None
    """
    _ = kwargs
    normalized_light_type = str(light_type).lower() if light_type is not None else "usb"
    if normalized_light_type != "usb":
        g.logger.warning(f"三色灯类型 {light_type} 已不再支持，自动切换为 usb")

    try:
        return UsbTricolourLight()
    except Exception as e:
        g.logger.error(f"创建USB三色灯失败: {e}")
        return None

