# unraid-fp — Unraid 机箱小屏状态面板

把 Unraid 机器接的显示器 / 小屏（framebuffer `/dev/fb0`）变成实时状态面板：CPU 与内存、阵列与校验进度、各盘温度与容量、Docker 容器、虚拟机、Emby 正在播放、网络吞吐。直接往 `fb0` 画像素，不需要 X / Wayland，也不占用 Unraid WebUI。

## 架构：宿主机 + 容器两件套

```
宿主机 host-agent.sh（每 8 秒）
        └──> data/state.json  ──挂载──>  容器内 dash.py 渲染  ──>  /dev/fb0
```

容器里拿不到 `mdcmd` / `smartctl` / `virsh` / `docker`，所以**数据采集放在宿主机**，容器只负责渲染。只跑容器会是一片空白——这是最常见的踩坑点。

## 仓库结构

| 路径 | 作用 |
|---|---|
| `dash.py` | 渲染主程序（容器内运行） |
| `Dockerfile` | 镜像构建（python:3.12-slim + pillow） |
| `host/host-agent.sh` | 宿主机数据采集，输出 `state.json` |
| `host/agent-daemon.sh` | 8 秒循环调用 `host-agent.sh` |
| `host/fp-takeover.sh` | 从 Unraid 控制台接管 `fb0`（关掉内核光标闪烁） |
| `host/fp-keepalive.sh` | 保活：控制台被抢回时重新接管 |
| `examples/layout.json` | 布局与配色配置示例 |

## 部署

### 1. 放文件

```bash
mkdir -p /mnt/user/appdata/unRAID-FP/{data,logs,fonts}
# 把 host/*.sh 放进 /mnt/user/appdata/unRAID-FP/
chmod +x /mnt/user/appdata/unRAID-FP/*.sh
```

`host-agent.sh` 里的 `OUT` 默认写 `/mnt/cache/appdata/unRAID-FP/data/state.json`，路径不同就改这一行。

### 2. 开机自启（关键）

写进 `/boot/config/go`，**必须放在 `/usr/local/sbin/emhttp` 之前**：

```bash
nohup /mnt/user/appdata/unRAID-FP/fp-takeover.sh >/dev/null 2>&1 &
nohup /mnt/user/appdata/unRAID-FP/fp-keepalive.sh >/dev/null 2>&1 &
nohup /mnt/user/appdata/unRAID-FP/agent-daemon.sh >/dev/null 2>&1 &
```

`emhttp` 内部调用的是前台阻塞进程、永不返回，写在它后面的启动项永远不会执行——这个坑踩过一次，整块屏幕黑屏。

### 3. 中文字体

面板需要文泉驿正黑：

```bash
# 容器要挂进去的字体文件
/mnt/user/appdata/unRAID-FP/fonts/wqy-zenhei.ttc
```

来源随意（系统包、字库站都行），文件名对得上即可。

### 4. 启动容器

```bash
docker run -d \
  --name unRAID-FP \
  --restart unless-stopped \
  --device /dev/fb0:/dev/fb0 \
  -e TZ=Asia/Shanghai \
  -e _REFRESH=8 \
  -e _EMBY_HOST=http://10.0.0.2:8097 \
  -e EMBY_DB=/emby-data/authentication.db \
  -v /mnt/user/appdata/unRAID-FP/dash.py:/app/dash.py:ro \
  -v /mnt/user/appdata/unRAID-FP/data:/app/data:ro \
  -v /mnt/user/appdata/unRAID-FP/logs:/app/logs \
  -v /mnt/user/appdata/unRAID-FP/fonts/wqy-zenhei.ttc:/app/fonts/wqy-zenhei.ttc:ro \
  -v /mnt/user/appdata/emby/data/authentication.db:/emby-data/authentication.db:ro \
  -v /sys:/sys:ro \
  lllpx579/unraid-fp:latest
```

`-v /sys:/sys:ro` 用于读取 CPU 温度 / 频率；`authentication.db` 用来拿 Emby 活跃 token，从而读到「谁在播什么」（多客户端各自的 token 可见会话不同，代码会遍历全部活跃 token 合并去重）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `_EMBY_HOST` / `EMBY_HOST` | `http://127.0.0.1:8097` | Emby 源站，不带端口会补 `:8097` |
| `EMBY_DB` | `/emby-data/authentication.db` | Emby 数据库路径，用于取活跃 token |
| `_REFRESH` | `8` | 画面刷新间隔（秒） |
| `TZ` | `Asia/Shanghai` | 时区 |

## 布局与配色

`data/layout.json` 控制边距、海报位置、静态/动画帧率与整套配色。字段非法会自动回退内置默认值，改完 `docker restart unRAID-FP` 生效。示例见 [examples/layout.json](examples/layout.json)。

配色以 `[R, G, B]` 给出，写入 `fb0` 时按 DRM XR24 的 `BGRX` 字节序落盘——Unraid 的 `fb0` 是 little-endian，直接把 `RGB` 丢进去会红蓝互换（橙色变蓝）。

## 已知限制

- 磁盘卡片的排布（`缓存 / 校验盘 / 硬盘1 / 硬盘2`）在 `host-agent.sh` 里写死，按自己的机箱改成实际盘位；
- 需要一个可写的 `fb0`（Unraid 直连显示器、或主板 iGPU 输出的小屏），分辨率和缩放要自己核对；
- 只在 Unraid 6.12 / 7.x + x86_64 上验证过。

## 许可

个人自用项目，未附开源协议。要用在商业场景先联系仓库作者。
