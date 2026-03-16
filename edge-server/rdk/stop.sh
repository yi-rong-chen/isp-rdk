#!/bin/bash

PROCESS_NAME="$1"

if [ -z "$PROCESS_NAME" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PROCESS_NAME="$(basename "$(dirname "$SCRIPT_DIR")")"
fi

stop_process() {
  local script_name="$1"
  local pattern="python3 .*${script_name}\\.pyc? --name ${PROCESS_NAME}"

  echo "Checking for: $pattern"
  PID_ARR=$(ps -aux | grep -E "$pattern" | grep -v grep | awk '{print $2}')
  if [ -n "$PID_ARR" ]; then
    for PID_VALUE in ${PID_ARR}; do
      echo "kill -9 $PID_VALUE (${script_name})"
      kill -9 "$PID_VALUE"
    done
  else
    echo "${script_name} not running"
  fi
}

stop_process "rdk_service"
