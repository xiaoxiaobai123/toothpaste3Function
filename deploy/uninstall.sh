#!/usr/bin/env bash
# 卸载脚本（debug 用，回退到部署前状态）
#
# 停服务、禁用自启、恢复旧 rgb565 文件（如果有备份）。
# 不删 /home/pi/main，那个手动处理。

set -euo pipefail

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }

log "停服务"
sudo systemctl stop main.service 2>/dev/null || true
sudo systemctl stop image_updater.service 2>/dev/null || true

log "禁用自启"
sudo systemctl disable main.service 2>/dev/null || true
sudo systemctl disable image_updater.service 2>/dev/null || true

log "移除 systemd 配置"
sudo rm -f /etc/systemd/system/main.service
sudo rm -f /etc/systemd/system/image_updater.service
sudo systemctl daemon-reload

log "移除软链接"
if [[ -L /home/pi/output_image.rgb565 ]]; then
    sudo rm /home/pi/output_image.rgb565
fi

# 若有备份，恢复最新的一份
LATEST_BAK=$(ls -t /home/pi/output_image.rgb565.bak.* 2>/dev/null | head -1 || true)
if [[ -n "$LATEST_BAK" ]]; then
    log "恢复最新备份 $LATEST_BAK"
    sudo mv "$LATEST_BAK" /home/pi/output_image.rgb565
fi

log "清理 tmpfs 临时文件"
sudo rm -f /dev/shm/output_image.rgb565

log "完成"
echo "注意：/home/pi/main 和 /home/pi/image_updater 二进制没删，按需手动处理。"
