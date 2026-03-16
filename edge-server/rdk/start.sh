#!/bin/bash

PROCESS_NAME="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
RUNTIME_DIR="$SCRIPT_DIR"

if [ -z "$PROCESS_NAME" ]; then
  PROCESS_NAME="$(basename "$PARENT_DIR")"
fi

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
  local pattern="python3 .*${script_name}\\.pyc? --name ${PROCESS_NAME}"

  PID_ARR=$(ps -aux | grep -E "$pattern" | grep -v grep | awk '{print $2}')
  if [ -n "$PID_ARR" ]; then
    for PID_VALUE in ${PID_ARR}; do
      echo "kill -9 $PID_VALUE (${script_name})"
      kill -9 "$PID_VALUE"
    done
  fi
}

ENTRY_BASE="${RUNTIME_DIR}/rdk_service"

stop_process "rdk_service"

ENTRY_SCRIPT=$(resolve_entry "$ENTRY_BASE")
if [ -z "$ENTRY_SCRIPT" ]; then
  echo "入口文件不存在: ${ENTRY_BASE}.py/.pyc"
  exit 1
fi

cd "$RUNTIME_DIR" || exit 1
sleep 1
nohup python3 "$(basename "$ENTRY_SCRIPT")" --name "$PROCESS_NAME" > /dev/null 2>&1 &
