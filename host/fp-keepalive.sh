#!/bin/bash
# fp-keepalive.sh —— 保证 HDMI 小屏常亮、面板独占 framebuffer
#
# 背景：内核的虚拟终端(vtcon)和屏幕休眠机制会在运行过程中重新绑定/熄屏，
#       而 fp-takeover.sh 只在开机跑一次，事后无人纠正，导致黑屏。
# 本脚本常驻运行，每 15 秒巡检一次，发现被改回去立即改回来。
#
# 启动方式（幂等，重复执行不会起两个）：
#   setsid nohup bash /mnt/cache/appdata/unRAID-FP/fp-keepalive.sh >/dev/null 2>&1 &

LOG=/var/log/unraid-fp-keepalive.log
FB=/sys/class/graphics/fb0
VC1=/sys/class/vtconsole/vtcon1/bind

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

log "=== keepalive start (pid $$) ==="

while true; do
    changed=""

    # 1) 虚拟终端接管 framebuffer -> 解绑
    cur=$(cat "$VC1" 2>/dev/null)
    if [ "$cur" != "0" ]; then
        echo 0 > "$VC1" 2>/dev/null && changed="$changed vtcon1:$cur->0"
    fi

    # 2) 屏幕休眠 -> 唤醒保持常亮
    cur=$(cat "$FB/blank" 2>/dev/null)
    if [ "$cur" != "0" ]; then
        echo 0 > "$FB/blank" 2>/dev/null && changed="$changed blank:$cur->0"
    fi

    # 3) 控制台光标 -> 关闭
    cur=$(cat /sys/class/graphics/fbcon/cursor_blink 2>/dev/null)
    if [ -n "$cur" ] && [ "$cur" != "-1" ]; then
        echo -1 > /sys/class/graphics/fbcon/cursor_blink 2>/dev/null \
            && changed="$changed cursor:$cur->-1"
    fi

    # 4) framebuffer 控制台旋转 -> 归零
    cur=$(cat /sys/class/graphics/fbcon/rotate 2>/dev/null)
    if [ -n "$cur" ] && [ "$cur" != "0" ]; then
        echo 0 > /sys/class/graphics/fbcon/rotate 2>/dev/null \
            && changed="$changed rotate:$cur->0"
    fi

    # 5) 面板容器没跑就拉起来
    st=$(docker inspect -f '{{.State.Running}}' unRAID-FP 2>/dev/null)
    if [ "$st" != "true" ]; then
        log "container not running (state=$st) -> start"
        docker start unRAID-FP >/dev/null 2>&1 && changed="$changed container:start"
    fi

    if [ -n "$changed" ]; then
        log "corrected:$changed"
    fi

    sleep 15
done
