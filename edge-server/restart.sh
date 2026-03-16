#!/bin/bash

resolve_entry() {
    local base_path="$1"
    if [ -f "${base_path}.pyc" ]; then
        echo "${base_path}.pyc"
        return 0
    fi
    if [ -f "${base_path}.py" ]; then
        echo "${base_path}.py"
        return 0
    fi
    return 1
}

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

# 4. 启动 python3 main.py/.pyc --name <dir_name>
echo ""
MAIN_ENTRY=$(resolve_entry "$CURRENT_DIR/main")
if [ -n "$MAIN_ENTRY" ]; then
    echo "启动 python3 $(basename "$MAIN_ENTRY") --name $DIR_NAME..."
    cd "$CURRENT_DIR"
    nohup env ISP_RESTART_TRIGGER=1 python3 "$(basename "$MAIN_ENTRY")" --name "$DIR_NAME" > /dev/null 2>&1 &
    echo "  - 已启动 $(basename "$MAIN_ENTRY") --name $DIR_NAME"
else
    echo "  - 错误: $CURRENT_DIR/main.py 和 $CURRENT_DIR/main.pyc 都不存在"
    exit 1
fi

echo ""
echo "========================================"
echo "重启完成！"
echo "========================================"
