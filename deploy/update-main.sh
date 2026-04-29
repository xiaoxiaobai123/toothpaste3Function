#!/usr/bin/env bash
# 更新 main 二进制（日常发版）
#
# 用法（在 NanoPi 上跑）：
#   1. 把新 main scp 到 NanoPi 某处，例如 /tmp/main.new
#      scp dist/main pi@<NANOPI_IP>:/tmp/main.new
#
#   2. 跑这个脚本
#      cd deploy && ./update-main.sh /tmp/main.new
#
# 自动备份旧版本 + 重启 + 失败回滚。

set -euo pipefail

NEW_BINARY="${1:-}"

if [[ -z "$NEW_BINARY" || ! -f "$NEW_BINARY" ]]; then
    echo "用法: $0 <path-to-new-main>"
    echo "例:  $0 /tmp/main.new"
    exit 1
fi

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m  ✗\033[0m %s\n' "$*" >&2; }

BACKUP="/home/pi/main.bak.$(date +%Y%m%d-%H%M%S)"

# -----------------------------------------------------------------
log "1/5  停 main"
sudo systemctl stop main.service
ok "main 已停"

# -----------------------------------------------------------------
log "2/5  备份当前 main -> $BACKUP"
sudo cp /home/pi/main "$BACKUP"
ok "已备份"

# -----------------------------------------------------------------
log "3/5  替换 main"
sudo cp "$NEW_BINARY" /home/pi/main
sudo chmod +x /home/pi/main
ok "已替换"

# -----------------------------------------------------------------
log "4/5  启动新 main"
sudo systemctl start main.service
sleep 6
ok "启动命令已下发"

# -----------------------------------------------------------------
log "5/5  健康检查"
if sudo systemctl is-active --quiet main.service; then
    # 再检查日志里没有 traceback
    if tail -50 /home/pi/my_app.log 2>/dev/null | grep -qE "Traceback|FATAL|ModuleNotFoundError"; then
        err "日志里有严重错误，回滚"
        sudo systemctl stop main.service
        sudo cp "$BACKUP" /home/pi/main
        sudo systemctl start main.service
        exit 1
    fi
    ok "main 启动成功，新版本生效"
    echo
    echo "观察 5 分钟再离开："
    echo "  tail -f /home/pi/my_app.log"
else
    err "main 启动失败，自动回滚"
    sudo systemctl stop main.service 2>/dev/null || true
    sudo cp "$BACKUP" /home/pi/main
    sudo systemctl start main.service
    sleep 3
    if sudo systemctl is-active --quiet main.service; then
        err "已回滚到旧版本，请排查新版问题"
    else
        err "回滚后仍失败，需人工介入！旧 main 在 $BACKUP"
    fi
    exit 1
fi
