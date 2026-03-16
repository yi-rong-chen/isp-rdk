#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sys
import inspect

# 添加当前目录到Python路径，以便导入nacos_var模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    import src.core.config.nacos_var as nacos_var
except ImportError as e:
    print(f"无法导入 nacos_var 模块: {e}")
    sys.exit(1)

def get_global_variables_in_declaration_order(module):
    """
    按照变量在源文件中的声明顺序获取全局变量
    """
    import re
    
    # 获取模块文件路径
    module_file = inspect.getfile(module)
    
    # 读取源文件
    try:
        with open(module_file, 'r', encoding='utf-8') as f:
            source_code = f.read()
    except Exception as e:
        print(f"❌ 无法读取源文件 {module_file}: {e}")
        return {}
    
    # 使用正则表达式找到全局变量声明（简单的赋值语句）
    # 匹配形如 "VARIABLE_NAME = " 的模式，只在顶级作用域中
    variable_pattern = r'^([A-Z_][A-Z0-9_]*)\s*='
    
    declared_vars = []
    in_function = False
    
    for line_num, line in enumerate(source_code.split('\n'), 1):
        original_line = line
        line = line.strip()
        
        # 跳过空行
        if not line:
            continue
            
        # 跳过注释行
        if line.startswith('#'):
            continue
        
        # 检测函数定义（缩进为0的def）
        if original_line.startswith('def '):
            in_function = True
            continue
        
        # 检测是否回到顶级作用域（非缩进行）
        if not original_line.startswith(' ') and not original_line.startswith('\t'):
            in_function = False
            
        # 只在顶级作用域中匹配变量声明
        if not in_function:
            match = re.match(variable_pattern, line)
            if match:
                var_name = match.group(1)
                # 去重：只添加第一次出现的变量
                if var_name not in declared_vars:
                    declared_vars.append(var_name)
                    print(f"📝 发现变量声明: 第{line_num}行 - {var_name}")
    
    # 需要排除的模块名称
    excluded_modules = {
        'os', 'json', 'requests', 'ast', 'g', 'throw_error'
    }
    
    # 按声明顺序获取变量值
    ordered_vars = {}
    for var_name in declared_vars:
        # 跳过排除的模块
        if var_name in excluded_modules:
            continue
            
        try:
            if hasattr(module, var_name):
                value = getattr(module, var_name)
                
                # 跳过函数和类
                if inspect.isfunction(value) or inspect.isclass(value) or inspect.ismodule(value):
                    continue
                    
                ordered_vars[var_name] = value
                print(f"✅ 获取变量值: {var_name} = {type(value).__name__}")
            else:
                print(f"⚠ 警告: 模块中不存在变量 {var_name}")
                
        except Exception as e:
            print(f"⚠ 警告: 无法获取变量 {var_name}: {e}")
            continue
    
    return ordered_vars

def format_value_to_string(value):
    """
    将Python变量转换为JSON中的字符串格式
    基于值的类型进行通用转换，无硬编码变量名
    """
    # 布尔值转换为字符串
    if isinstance(value, bool):
        return str(value)
    
    # 字符串类型处理
    elif isinstance(value, str):
        # 空字符串处理
        if value == "":
            return '""'
        # 包含换行符的字符串（通常是代码块）
        elif '\n' in value or (value.startswith('def ') and value.count('\n') > 0):
            return f'"""\n{value}\n"""'
        # 多行字符串但没有实际内容（如空的代码块）
        elif value.strip() == '':
            return "'''\n'''"
        # 普通字符串
        else:
            return f'"{value}"'
    
    # 列表类型处理
    elif isinstance(value, list):
        # 空列表
        if len(value) == 0:
            return "[]"
        # 非空列表
        else:
            return json.dumps(value, ensure_ascii=False, indent=2)
    
    # 字典类型处理
    elif isinstance(value, dict):
        # 空字典
        if len(value) == 0:
            return "{}"
        # 非空字典
        else:
            return json.dumps(value, ensure_ascii=False, indent=2)
    
    # 数字类型（整数、浮点数）
    elif isinstance(value, (int, float)):
        return str(value)
    
    # None 值
    elif value is None:
        return "null"
    
    # 其他类型，尝试 JSON 序列化
    else:
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            # 如果无法序列化，转换为字符串
            return f'"{str(value)}"'

def generate_isp_json():
    """
    生成isp.json文件 - 按照变量声明顺序读取所有全局变量
    """
    print("🔍 正在按声明顺序扫描 nacos_var 模块中的全局变量...")
    
    # 按照变量声明顺序获取模块中的所有全局变量
    global_vars = get_global_variables_in_declaration_order(nacos_var)
    
    if not global_vars:
        print("❌ 未发现任何全局变量")
        return False
    
    print(f"📊 共发现 {len(global_vars)} 个全局变量")
    print("-" * 50)
    
    # 构建配置字典（保持声明顺序）
    config_dict = {}
    
    # 按照声明顺序处理所有变量
    for var_name, value in global_vars.items():
        config_dict[var_name] = format_value_to_string(value)
        print(f"✓ 已处理变量: {var_name}")
    
    # 生成JSON文件
    output_file = 'isp.json'
    
    try:
        # 检查文件是否存在
        if os.path.exists(output_file):
            print(f"📄 文件 {output_file} 已存在，将进行覆盖")
        
        # 写入JSON文件
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=4)
        
        print(f"✅ 成功生成 {os.path.abspath(output_file)}")
        print(f"📊 共写入 {len(config_dict)} 个配置项")
        
        # 显示生成的文件大小
        file_size = os.path.getsize(output_file)
        print(f"📦 文件大小: {file_size} 字节")
        
    except Exception as e:
        print(f"❌ 生成 {output_file} 时出错: {str(e)}")
        return False
    
    return True

def main():
    """
    主函数 - 按照声明顺序读取 nacos_var.py 中的所有全局变量并生成 isp.json
    """
    print("🚀 开始按声明顺序生成 isp.json 文件...")
    print("📋 将按照 nacos_var.py 中变量的声明顺序处理所有全局变量")
    print("=" * 50)
    
    success = generate_isp_json()
    
    print("=" * 50)
    if success:
        print("🎉 isp.json 文件生成完成！")
    else:
        print("💥 isp.json 文件生成失败！")
        sys.exit(1)

if __name__ == "__main__":
    main()
