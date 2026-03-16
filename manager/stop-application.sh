#!/bin/bash

# 基础路径配置
EDGE_SERVER_BASE="/home/ccai/edge/edge-server"
EDGE_CONSOLE="/home/ccai/edge/edge-console"

stop_process() {
  local script_name="$1"
  local device_name="$2"
  local pattern="python3 .*${script_name}\\.pyc? --name ${device_name}"

  echo "停止 ${script_name}.py/.pyc --name ${device_name} 进程..."
  PID_ARR=$(ps -aux | grep -E "$pattern" | grep -v grep | awk '{print $2}')
  if [ -n "$PID_ARR" ]; then
    for PID_VALUE in ${PID_ARR}; do
      echo "  - 杀死 ${script_name}.py/.pyc --name ${device_name} (PID: $PID_VALUE)"
      kill -9 $PID_VALUE
    done
  else
    echo "  - ${script_name}.py/.pyc --name ${device_name} 未运行"
  fi
}

stop_inference_processes() {
  local device_name="$1"
  stop_process "rdk_service" "$device_name"
}

echo "========================================"
echo "开始查找有效的实例目录..."
echo "========================================"

# 查找所有有效的数字目录
VALID_DIRS=()
for dir in "$EDGE_SERVER_BASE"/*; do
  if [ -d "$dir" ]; then
    dirname=$(basename "$dir")
    # 检查是否为数字目录（0-20范围内）
    if [[ "$dirname" =~ ^[0-9]+$ ]] && [ "$dirname" -le 20 ]; then
      VALID_DIRS+=("$dirname")
    fi
  fi
done

# 按数字顺序排序
IFS=$'\n' VALID_DIRS=($(sort -n <<<"${VALID_DIRS[*]}"))
unset IFS

if [ ${#VALID_DIRS[@]} -eq 0 ]; then
  echo "警告: 未找到有效的实例目录"
else
  echo "找到的有效实例目录: ${VALID_DIRS[@]}"
fi

echo ""
echo "========================================"
echo "开始停止所有服务..."
echo "========================================"

# 停止所有实例的进程
for name in "${VALID_DIRS[@]}"; do
  echo "停止实例 $name 的进程..."

  stop_process "main" "$name"
  stop_inference_processes "$name"
done

# 停止前端服务
echo "停止前端服务..."
PID_ARR=$(ps -aux | grep "serve -s dist -l 8000" | grep -v grep | awk '{print $2}')
if [ -n "$PID_ARR" ]; then
  for PID_VALUE in ${PID_ARR}; do
    echo "  - 杀死 serve (PID: $PID_VALUE)"
    kill -9 $PID_VALUE
  done
else
  echo "  - serve 未运行"
fi
