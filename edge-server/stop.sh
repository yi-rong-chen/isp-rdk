#!/bin/bash

stop_process() {
    local script_name="$1"
    local device_name="$2"
    local pattern="python3 .*${script_name}\\.pyc? --name ${device_name}"

    echo "停止 python3 ${script_name}.py/.pyc --name $device_name 进程..."
    PID_ARR=$(ps -aux | grep -E "$pattern" | grep -v grep | awk '{print $2}')
    if [ -n "$PID_ARR" ]; then
        for PID_VALUE in ${PID_ARR}; do
            echo "  - 杀死 ${script_name}.py/.pyc --name $device_name (PID: $PID_VALUE)"
            kill -9 $PID_VALUE
        done
    else
        echo "  - ${script_name}.py/.pyc --name $device_name 未运行"
    fi
}

stop_inference_processes() {
    local device_name="$1"
    stop_process "rdk_service" "$device_name"
}

# 获取当前执行目录的绝对路径
CURRENT_DIR=$(pwd)
# 获取当前目录的文件夹名称
DIR_NAME=$(basename "$CURRENT_DIR")

echo "========================================"
echo "当前目录: $CURRENT_DIR"
echo "目录名称: $DIR_NAME"
echo "========================================"

# 1. 删除当前目录下的isp.json文件
echo ""
echo "删除 isp.json 文件..."
if [ -f "$CURRENT_DIR/isp.json" ]; then
    rm -f "$CURRENT_DIR/isp.json"
    echo "  - 已删除 isp.json"
else
    echo "  - isp.json 不存在，跳过"
fi

# 2. 停止 python3 main.py/.pyc --name <dir_name> 进程
echo ""
stop_process "main" "$DIR_NAME"

# 3. 停止独立推理进程
echo ""
stop_inference_processes "$DIR_NAME"

# 等待进程完全停止
echo ""
echo "等待进程完全停止..."
sleep 2

echo ""
echo "========================================"
echo "停止完成！"
echo "========================================"
