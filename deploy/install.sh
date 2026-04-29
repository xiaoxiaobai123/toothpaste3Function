#!/usr/bin/env bash
# 首次部署脚本（或重装环境时用）
#
# 前提：已经把 main 和 image_updater 两个二进制放到 /home/pi/
#
# 用法：
#   cd <此脚本所在目录>
#   ./install.sh
#
# 幂等的 —— 可以反复跑，不会搞坏已有配置。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m  ✗\033[0m %s\n' "$*" >&2; }

# -----------------------------------------------------------------
# 1. 环境检查
# -----------------------------------------------------------------
log "Step 1/8  环境检查"

if [[ ! -f /home/pi/main ]]; then
    err "/home/pi/main 不存在，先 scp 二进制再来"
    exit 1
fi
if [[ ! -f /home/pi/image_updater ]]; then
    err "/home/pi/image_updater 不存在"
    exit 1
fi
ok "二进制就绪"

if [[ ! -e /dev/fb0 ]]; then
    err "/dev/fb0 不存在，没有 framebuffer，显示会失败"
fi

# -----------------------------------------------------------------
# 2. 停旧服务
# -----------------------------------------------------------------
log "Step 2/8  停旧服务（如果在跑）"
sudo systemctl stop main.service 2>/dev/null || true
sudo systemctl stop image_updater.service 2>/dev/null || true
sudo pkill -f /home/pi/main 2>/dev/null || true
sudo pkill -f /home/pi/image_updater 2>/dev/null || true
ok "旧进程清理完毕"

# -----------------------------------------------------------------
# 3. 备份 + 处理 rgb565 文件
# -----------------------------------------------------------------
log "Step 3/8  处理 output_image.rgb565（tmpfs 软链接）"

# 如果 /home/pi/output_image.rgb565 存在且是真实文件（不是软链接），备份
if [[ -f /home/pi/output_image.rgb565 && ! -L /home/pi/output_image.rgb565 ]]; then
    BAK="/home/pi/output_image.rgb565.bak.$(date +%Y%m%d-%H%M%S)"
    sudo mv /home/pi/output_image.rgb565 "$BAK"
    ok "老真实文件备份到 $BAK"
fi

# 建 tmpfs 文件
sudo touch /dev/shm/output_image.rgb565
sudo chmod 666 /dev/shm/output_image.rgb565
ok "tmpfs 文件 /dev/shm/output_image.rgb565 就绪"

# 建（或更新）软链接
sudo ln -sfn /dev/shm/output_image.rgb565 /home/pi/output_image.rgb565
sudo chown -h pi:pi /home/pi/output_image.rgb565
ok "软链接 /home/pi/output_image.rgb565 -> /dev/shm/output_image.rgb565"

# -----------------------------------------------------------------
# 4. 安装 systemd service 文件
# -----------------------------------------------------------------
log "Step 4/8  安装 systemd service"

sudo cp "$SCRIPT_DIR/main.service" /etc/systemd/system/main.service
sudo cp "$SCRIPT_DIR/image_updater.service" /etc/systemd/system/image_updater.service
ok "service 文件复制完成"

sudo systemctl daemon-reload
ok "systemd daemon-reload"

# -----------------------------------------------------------------
# 5. 启用开机自启
# -----------------------------------------------------------------
log "Step 5/8  启用开机自启"
sudo systemctl enable main.service >/dev/null 2>&1
sudo systemctl enable image_updater.service >/dev/null 2>&1
ok "两个服务都已 enable"

# -----------------------------------------------------------------
# 6. 启动服务
# -----------------------------------------------------------------
log "Step 6/8  启动服务"
sudo systemctl start main.service
sleep 2
sudo systemctl start image_updater.service
sleep 8
ok "服务启动命令已下发，等待就绪"

# -----------------------------------------------------------------
# 7. 健康检查
# -----------------------------------------------------------------
log "Step 7/8  健康检查"

if sudo systemctl is-active --quiet main.service; then
    ok "main.service: active"
else
    err "main.service 没跑起来，看日志：sudo journalctl -u main --no-pager"
fi

if sudo systemctl is-active --quiet image_updater.service; then
    ok "image_updater.service: active"
else
    err "image_updater.service 没跑起来，看日志：sudo journalctl -u image_updater --no-pager"
fi

# 检查 tmpfs 文件是否被写入
sleep 5
if [[ -s /dev/shm/output_image.rgb565 ]]; then
    ok "tmpfs 文件已有数据（$(stat -c %s /dev/shm/output_image.rgb565) 字节）"
else
    err "tmpfs 文件还是空的，main 可能没在写；或者还没到第一次拍照"
fi

# -----------------------------------------------------------------
# 8. 完成
# -----------------------------------------------------------------
log "Step 8/8  完成"
echo
echo "常用命令："
echo "  查看视觉程序日志：  tail -f /home/pi/my_app.log"
echo "  查看 C 显示日志：   sudo journalctl -u image_updater -f"
echo "  重启视觉程序：      sudo systemctl restart main.service"
echo "  重启显示程序：      sudo systemctl restart image_updater.service"
echo "  更新 main 二进制：  ./update-main.sh <path-to-new-main>"
echo
