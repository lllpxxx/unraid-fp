#!/bin/bash
# 1024x600 HDMI 小屏面板 —— 开机接管屏幕
# 由 /boot/config/go 以 nohup 方式拉起，无需人工干预。
#
# 关键点：
#   - 屏幕方向由 dash.py 内部 rotate(-90) 完成，转成 1024x600 后写 fb0
#   - fbcon 的 rotate 必须保持 0，否则控制台文字方向会和面板打架
#   - 容器内 /sys 是只读挂载，接管动作只能由宿主机做

LOG=/var/log/unraid-fp-takeover.log
PANEL=unRAID-FP

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

log "=== takeover start ==="

# 1) 等 Docker 服务就绪（最多 120 秒）
for i in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 2
done
docker info >/dev/null 2>&1 || { log "docker not ready, abort"; exit 1; }
log "docker ready"

# 2) 等面板容器自身跑起来（restart=unless-stopped 会自动拉起）
for i in $(seq 1 30); do
    [ "$(docker inspect -f '{{.State.Running}}' $PANEL 2>/dev/null)" = "true" ] && break
    sleep 2
done
log "panel running=$(docker inspect -f '{{.State.Running}}' $PANEL 2>/dev/null)"

# 3) 解绑 fbcon / 关闭光标 / 取消断电，并持续复查 60 秒
#    emhttp 或后续服务可能把 vtcon1 重新绑上，绑回来就再解一次。
END=$(( $(date +%s) + 60 ))
while [ "$(date +%s)" -lt "$END" ]; do
    # 关光标
    [ "$(cat /sys/class/graphics/fbcon/cursor_blink 2>/dev/null)" != "-1" ] && \
        echo 0 > /sys/class/graphics/fbcon/cursor_blink 2>/dev/null

    # fbcon 方向保持 0（竖屏原生）；面板自己旋转后输出 1024x600
    [ "$(cat /sys/class/graphics/fbcon/rotate 2>/dev/null)" != "0" ] && \
        echo 0 > /sys/class/graphics/fbcon/rotate 2>/dev/null

    # 解绑 frame buffer console —— 决定面板能否独占屏幕
    if [ "$(cat /sys/class/vtconsole/vtcon1/bind 2>/dev/null)" = "1" ]; then
        echo 0 > /sys/class/vtconsole/vtcon1/bind 2>/dev/null
        log "unbound vtcon1"
    fi

    # 屏幕不许休眠
    if [ "$(cat /sys/class/graphics/fb0/blank 2>/dev/null)" != "0" ]; then
        echo 0 > /sys/class/graphics/fb0/blank 2>/dev/null
        log "blank -> 0"
    fi

    sleep 2
done

# 4) 最终状态核对
BIND=$(cat /sys/class/vtconsole/vtcon1/bind 2>/dev/null)
BLANK=$(cat /sys/class/graphics/fb0/blank 2>/dev/null)
CUR=$(cat /sys/class/graphics/fbcon/cursor_blink 2>/dev/null)
ROT=$(cat /sys/class/graphics/fbcon/rotate 2>/dev/null)
SIZE=$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)
FORMAT=$(cat /sys/kernel/debug/dri/0/framebuffer 2>/dev/null | grep -m1 format)
log "final: vtcon1.bind=$BIND fb0.blank=$BLANK cursor=$CUR rotate=$ROT size=$SIZE"
log "framebuffer: $FORMAT"

# 5) 解绑成功才重启面板，确保它在 fbcon 让位之后接管屏幕
if [ "$BIND" = "0" ]; then
    docker restart $PANEL >/dev/null 2>&1
    log "panel restarted"

    # 6) 等面板画出第一帧后，抽查 fb0 是否真有内容（非全黑）
    sleep 12
    if docker exec $PANEL python3 -c "import sys;d=open('/dev/fb0','rb').read(4096);sys.exit(0 if any(d) else 1)" 2>/dev/null; then
        log "fb0 has content — panel took over OK"
    else
        log "WARN: fb0 looks blank, check panel.log"
    fi
else
    log "ERROR: fbcon still bound, panel may be overwritten"
fi

log "=== takeover done ==="
