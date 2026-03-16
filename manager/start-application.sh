#!/bin/bash

# 基础路径配置
EDGE_SERVER_BASE="/home/ccai/edge/edge-server"
EDGE_CONSOLE="/home/ccai/edge/edge-console"

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

PID_ARR=$(ps -aux | grep "node bff.js" | grep -v grep | awk '{print $2}')
if [ -n "$PID_ARR" ]; then
  for PID_VALUE in ${PID_ARR}; do
    echo "  - 杀死 node bff.js (PID: $PID_VALUE)"
    kill -9 $PID_VALUE
  done
else
  echo "  - node bff.js 未运行"
fi

echo "所有服务已停止，等待2秒..."
sleep 2

echo ""
echo "========================================"
echo "开始启动所有服务..."
echo "========================================"

# 启动所有实例
for name in "${VALID_DIRS[@]}"; do
  echo "启动实例 $name..."

  MAIN_ENTRY=$(resolve_entry "$EDGE_SERVER_BASE/$name/main")
  if [ -n "$MAIN_ENTRY" ]; then
    cd "$EDGE_SERVER_BASE/$name"
    nohup python3 "$(basename "$MAIN_ENTRY")" --name "$name" > /dev/null 2>&1 &
    echo "  - 已启动 $(basename "$MAIN_ENTRY") --name $name"
  else
    echo "  - 警告: $EDGE_SERVER_BASE/$name/main.py 与 main.pyc 均不存在"
  fi
  
  sleep 2
done

# 启动前端服务
echo "启动前端服务..."
if [ -d "$EDGE_CONSOLE" ]; then
  cd "$EDGE_CONSOLE"
  nohup serve -s dist -l 8000 > /dev/null 2>&1 &
  echo "  - 已启动 serve"
else
  echo "  - 警告: $EDGE_CONSOLE 目录不存在"
fi

echo ""
echo '========================================'
echo '所有服务启动完成！'
echo '========================================'
