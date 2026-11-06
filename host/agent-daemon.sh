#!/bin/bash
# 每 8 秒刷新一次 state.json，供容器 unRAID-FP 读取
while true; do
    bash /mnt/cache/appdata/unRAID-FP/host-agent.sh >/dev/null 2>&1
    sleep 8
done
