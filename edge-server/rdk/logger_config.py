#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import logging.handlers
from datetime import datetime, timedelta
from pathlib import Path
import glob

class LoggerConfig:
    """日志配置类，支持按天存储和自动清理"""
    
    def __init__(self, log_dir="logs", max_days=3, log_level=logging.INFO, log_name="rdk"):
        self.log_dir = Path(log_dir)
        self.max_days = max_days
        self.log_level = log_level
        self.log_name = log_name
        
        # 创建日志目录
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 配置根日志器
        self._setup_root_logger()
        
        # 清理过期日志
        self._cleanup_old_logs()
    
    def _setup_root_logger(self):
        """设置根日志器"""
        # 创建根日志器
        root_logger = logging.getLogger()
        root_logger.setLevel(self.log_level)
        
        # 清除现有的处理器
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 创建格式器
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
        
        # 文件处理器（按天轮转）
        log_file = self.log_dir / f"{self.log_name}.log"
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_file),
            when='midnight',
            interval=1,
            backupCount=self.max_days,
            encoding='utf-8'
        )
        # 设置后缀格式（兼容旧版本Python）
        file_handler.suffix = '%Y-%m-%d'
        file_handler.setLevel(self.log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        
        # 错误日志单独文件
        error_log_file = self.log_dir / f"{self.log_name}_error.log"
        error_handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(error_log_file),
            when='midnight',
            interval=1,
            backupCount=self.max_days,
            encoding='utf-8'
        )
        # 设置后缀格式（兼容旧版本Python）
        error_handler.suffix = '%Y-%m-%d'
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        root_logger.addHandler(error_handler)
    
    def _cleanup_old_logs(self):
        """清理超过保留天数的日志文件"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.max_days)
            
            # 清理主日志文件
            log_pattern = str(self.log_dir / f"{self.log_name}.log.*")
            for log_file in glob.glob(log_pattern):
                file_time = datetime.fromtimestamp(os.path.getctime(log_file))
                if file_time < cutoff_date:
                    try:
                        os.remove(log_file)
                        print(f"[LOG] 删除过期日志文件: {log_file}")
                    except Exception as e:
                        print(f"[LOG] 删除日志文件失败 {log_file}: {e}")
            
            # 清理错误日志文件
            error_pattern = str(self.log_dir / f"{self.log_name}_error.log.*")
            for log_file in glob.glob(error_pattern):
                file_time = datetime.fromtimestamp(os.path.getctime(log_file))
                if file_time < cutoff_date:
                    try:
                        os.remove(log_file)
                        print(f"[LOG] 删除过期错误日志文件: {log_file}")
                    except Exception as e:
                        print(f"[LOG] 删除错误日志文件失败 {log_file}: {e}")
                        
        except Exception as e:
            print(f"[LOG] 清理过期日志时出错: {e}")
    
    def get_logger(self, name=None):
        """获取指定名称的日志器"""
        return logging.getLogger(name)
    
    def set_level(self, level):
        """设置日志级别"""
        self.log_level = level
        logging.getLogger().setLevel(level)
        for handler in logging.getLogger().handlers:
            handler.setLevel(level)

def get_logger(name=None, log_name="rdk"):
    """获取日志器的便捷函数"""
    # 为不同的日志名称创建不同的配置实例
    if not hasattr(get_logger, '_configs'):
        get_logger._configs = {}
    
    if log_name not in get_logger._configs:
        get_logger._configs[log_name] = LoggerConfig(log_name=log_name)
    
    return get_logger._configs[log_name].get_logger(name)
