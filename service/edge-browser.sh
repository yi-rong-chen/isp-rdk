#!/usr/bin/env bash
set -euo pipefail

USER_NAME=customer
URL="http://192.169.1.10:8000"

# 选择命令名（优先用非 snap 版）
CMD="/usr/bin/chromium-browser"
command -v "$CMD" >/dev/null 2>&1 || CMD="/usr/bin/chromium"
command -v "$CMD" >/dev/null 2>&1 || CMD="/snap/bin/chromium"

USER_UID=$(id -u "$USER_NAME")

# 找活动图形会话
SID=$(loginctl list-sessions --no-legend | awk -v u="$USER_NAME" '$3==u{print $1; exit}')
if [[ -z "${SID:-}" ]]; then
  echo "No active graphical session for $USER_NAME" >&2
  exit 1
fi

TYPE=$(loginctl show-session "$SID" -p Type --value || true)
DISP=$(loginctl show-session "$SID" -p Display --value || true)

export XDG_RUNTIME_DIR="/run/user/$USER_UID"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus"

COMMON_ARGS=(
  --disable-default-apps
  --disable-extensions
  --disable-plugins
  --start-maximized
  --incognito
)

GPU_ARGS=(
  --enable-gpu-rasterization
  --ignore-gpu-blocklist
)

# 选择以谁身份执行
run_as_target() {
  if [[ "$(id -u)" -eq "$USER_UID" ]]; then
    # 已经是 customer
    exec env \
      GOOGLE_API_KEY="no" \
      GOOGLE_DEFAULT_CLIENT_ID="no" \
      GOOGLE_DEFAULT_CLIENT_SECRET="no" \
      "$@"
  elif [[ "$EUID" -eq 0 ]]; then
    # root 用 runuser，不依赖 sudoers
    exec runuser -u "$USER_NAME" -- env \
      GOOGLE_API_KEY="no" \
      GOOGLE_DEFAULT_CLIENT_ID="no" \
      GOOGLE_DEFAULT_CLIENT_SECRET="no" \
      "$@"
  else
    # 其他用户（需要 sudo 权限）
    exec sudo -u "$USER_NAME" env \
      GOOGLE_API_KEY="no" \
      GOOGLE_DEFAULT_CLIENT_ID="no" \
      GOOGLE_DEFAULT_CLIENT_SECRET="no" \
      "$@"
  fi
}

if [[ "$TYPE" == "wayland" && -S "$XDG_RUNTIME_DIR/wayland-0" ]]; then
  echo "Launching on Wayland for $USER_NAME (UID=$USER_UID)..."
  run_as_target \
    XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
    WAYLAND_DISPLAY=wayland-0 \
    "$CMD" \
      --ozone-platform=wayland \
      "${COMMON_ARGS[@]}" \
      "${GPU_ARGS[@]}" \
      --app="$URL"
else
  DISPLAY_VAL="${DISP:-:0}"
  echo "Launching on X11 for $USER_NAME (UID=$USER_UID) DISPLAY=$DISPLAY_VAL..."
  run_as_target \
    DISPLAY="$DISPLAY_VAL" \
    XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
    "$CMD" \
      "${COMMON_ARGS[@]}" \
      "${GPU_ARGS[@]}" \
      --app="$URL"
fi

