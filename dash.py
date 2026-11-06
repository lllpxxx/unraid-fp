#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unRAID 1024x600 竖屏状态面板（顺时针90°旋转装屏）
布局顺序：头部 → 系统 → 阵列 → 网络 → 存储 → Docker → VM
"""
import subprocess, glob, os, time, re, signal, sys
# Pillow 装在 flash 的 pylibs 里（/usr/lib 是内存盘，重启会丢依赖）
_PYLIB = "/app/pylibs"
if os.path.isdir(_PYLIB) and _PYLIB not in sys.path:
    sys.path.insert(0, _PYLIB)

W,H = 1024,600
VW,VH = 600,1024
FB = "/dev/fb0"

# ---------------- 布局配置（layout.json）----------------
# 目标：调边距/颜色/帧率不用改代码，改完 docker restart 即可生效。
# 配置文件缺失或字段非法时，全部回退到下面的内置默认值，绝不影响渲染。
_LAYOUT_DEFAULT = {
    "margin_left":        16,      # 左边距
    "margin_right":        8,      # 右边距
    "gap":                 8,      # 卡片/海报间隙
    "poster_bottom_pad":  24,      # 海报底边相对卡片底部的额外下探(px)
    "fps_static":        1.2,      # 静止时帧率
    "fps_anim":         10.0,      # 有动画(滚动/曲线)时帧率
    "color_bg":      [14, 20, 34],   # 主背景
    "color_clr":     [40, 160, 240], # 顶部标题色
    "color_green":   [90, 235, 130],
    "color_orange":  [255, 175, 60],
    "color_red":     [255, 85, 85],
    "color_dim":     [168, 178, 198],
    "color_gray":    [178, 192, 212],
    "color_blue":    [120, 205, 255],
    "color_cyan":    [80, 215, 255],
}


def _layout_load():
    """读 layout.json，逐项校验；任何异常都静默回退默认值。"""
    cfg = {k: (list(v) if isinstance(v, list) else v)
           for k, v in _LAYOUT_DEFAULT.items()}
    try:
        import json as _lj
        with open("/app/data/layout.json", "r", encoding="utf-8") as f:
            raw = _lj.load(f)
        if isinstance(raw, dict):
            for k in list(cfg):
                if k not in raw:
                    continue
                v = raw[k]
                if isinstance(cfg[k], list):          # 颜色三元组
                    if (isinstance(v, (list, tuple)) and len(v) == 3
                            and all(isinstance(x, (int, float)) for x in v)):
                        cfg[k] = [max(0, min(255, int(x))) for x in v]
                elif isinstance(cfg[k], int):         # 整数
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        cfg[k] = int(v)
                elif isinstance(cfg[k], float):       # 浮点
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        cfg[k] = float(v)
    except Exception:
        pass
    return cfg


_LAYOUT = _layout_load()


# ==================== 数据采集 ====================

# ==================== 宿主机状态桥接 ====================
# 容器内无法调用 mdcmd/smartctl/virsh/docker，由宿主机 host-agent.sh
# 定期把数据汇总到 /app/data/state.json，这里只读该文件。
import json as _json2
_STATE_PATH = "/app/data/state.json"
_state_cache = {"t": 0, "v": None}

def hstate():
    """读宿主机状态（缓存 2 秒）。读不到返回空 dict。"""
    now = time.time()
    if now - _state_cache["t"] < 2 and _state_cache["v"] is not None:
        return _state_cache["v"]
    d = {}
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            d = _json2.load(f)
    except Exception:
        d = {}
    _state_cache["t"] = now
    _state_cache["v"] = d
    return d


_PROC_MAP = {
    "loadavg": "/tmp/_proc_loadavg",
    "meminfo": "/tmp/_proc_meminfo",
    "uptime":  "/tmp/_proc_uptime",
    "netdev":  "/tmp/_proc_netdev",
    "stat":    "/tmp/_proc_stat",
    "diskstats": "/tmp/_proc_diskstats",
}

_proc_synced_ts = {"ts": 0}

def _sync_proc():
    """把宿主机 state.json 里的 /proc 快照落到本地临时文件，
    让原有 open('/tmp/_proc_xxx') 逻辑无需大改即可拿到真实宿主数据。
    用 state.json 的 ts 判断是否重写，而不是文件 mtime。"""
    st = hstate()
    sysd = st.get("sys") or {}
    ts = st.get("ts") or 0
    if ts and ts == _proc_synced_ts["ts"]:
        return                      # 数据源没变，无需重写
    for k, path in _PROC_MAP.items():
        v = sysd.get(k)
        if not v:
            continue
        try:
            with open(path, "w") as f:
                f.write(v if v.endswith("\n") else v + "\n")
        except Exception:
            pass
    _proc_synced_ts["ts"] = ts

def cpu_temp():
    t=[]
    for f in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        try:
            if open(os.path.dirname(f)+"/name").read().strip()=="coretemp":
                t.append(int(open(f).read().strip())//1000)
        except: pass
    return (max(t), sum(t)//len(t)) if t else (0,0)

_last_cpu=None
def cpu_power():
    """CPU 实时功耗(W)，由 host-agent.sh 从 Intel RAPL 采集写入 state.json。"""
    try:
        v = (hstate().get("sys") or {}).get("cpu_w")
        if v not in (None, ""):
            return float(v)
    except Exception:
        pass
    return None


def cpu_usage():
    """CPU 占用率。

    优先用宿主机在采集时算好的 cpu_pct —— 容器渲染帧率远快于 state.json
    的 8 秒刷新周期，若在容器内做前后差值，绝大多数帧会拿到同一份数据，
    dt=0 导致恒返回 0（曾是 CPU 环一直显示 0 的原因）。
    """
    global _last_cpu
    try:
        _st = hstate().get("sys") or {}
        _p = _st.get("cpu_pct")
        if _p not in (None, ""):
            return max(0.0, min(100.0, float(_p)))
    except Exception:
        pass

    # 兜底：宿主机没给值时，退回自算差值
    _sync_proc()
    try:
        v=[int(x) for x in open("/tmp/_proc_stat").readline().split()[1:]]
    except Exception:
        v=[int(x) for x in open("/proc/stat").readline().split()[1:]]
    tot,idle=sum(v), v[3]+v[4]
    if _last_cpu is None:
        _last_cpu=(tot,idle); return 0.0
    dt,didle = tot-_last_cpu[0], idle-_last_cpu[1]
    _last_cpu=(tot,idle)
    if dt<=0: return 0.0
    return max(0.0,min(100.0,(1-didle/dt)*100))


_cpu_model=None
def cpu_model():
    global _cpu_model
    if _cpu_model is not None: return _cpu_model
    try:
        for l in open("/proc/cpuinfo"):
            if l.startswith("model name"):
                raw=l.split(":",1)[1].strip()
                m=re.search(r"(i[3579]-\d{4,5}[A-Z]*)",raw)
                _cpu_model = m.group(1) if m else raw
                break
    except: pass
    if not _cpu_model: _cpu_model="CPU"
    return _cpu_model

def load(): return open("/tmp/_proc_loadavg").read().split()[:3]

def meminfo():
    d={}
    for l in open("/tmp/_proc_meminfo"):
        d[l.split(":")[0]]=int(l.split()[1])
    tot=d["MemTotal"]; ava=d.get("MemAvailable",d["MemFree"])
    return tot,tot-ava

def mdstate():
    """返回 (阵列状态, 校验进度%)。数据来自宿主机 host-agent.sh 汇总的 state.json"""
    try:
        d = hstate().get("md") or {}
        st = d.get("mdState") or "?"
        p = 0
        try:
            rs = int(d.get("mdResync") or 0)
            sz = int(d.get("mdResyncSize") or 0)
            if sz > 0 and rs > 0:
                p = int(rs * 100 // sz)
        except Exception:
            p = 0
        return st, p
    except Exception:
        return "?", 0

_disk_cache={"t":0,"v":[],"extra":{}}
def disks_ini():
    """读 /var/local/emhttp/disks.ini（宿主机挂载），返回 dict(name -> fields)"""
    out={}
    try:
        cur=None
        for line in open("/var/local/emhttp/disks.ini",encoding="utf-8",errors="ignore"):
            line=line.strip()
            if line.startswith('["') and line.endswith('"]'):
                cur=line[2:-2]; out[cur]={}
            elif cur and "=" in line:
                k,v=line.split("=",1)
                out[cur][k.strip()]=v.strip().strip('"')
    except Exception:
        pass
    return out

def disks():
    """返回 [(显示名, 温度 或 None)]。None=硬盘休眠（不唤醒）
    数据来自宿主机 state.json（host-agent.sh 生成）"""
    d = hstate().get("disks") or []
    out = []
    for x in d:
        out.append((x.get("name", "?"), x.get("temp")))
    if out:
        return out
    return [("校验盘", None), ("硬盘1", None), ("硬盘2", None)]

def _disks_smart():
    """老逻辑保留：ini 温度缺失时用 smartctl 补（不唤醒休眠盘）"""
    r=[]
    for dev in ["sdc","sdd","sde"]:
        t=None
        try:
            # -n standby: 硬盘休眠时直接返回，绝不唤醒
            pr=subprocess.run(["smartctl","-n","standby","-A","/dev/"+dev],
                              capture_output=True,text=True,timeout=10)
            o=pr.stdout or ""
            if pr.returncode==2 or "STANDBY" in o.upper():
                t=None                      # 休眠，不唤醒
            else:
                mm=re.search(r"^\s*194\s+Temperature_Celsius\s+\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)",o,re.M|re.I)
                if not mm:
                    mm=re.search(r"^\s*190\s+Airflow_Temperature\S*\s+\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)",o,re.M|re.I)
                if not mm:
                    mm=re.search(r"Temperature\S*\s+.*?-\s+(\d+)\s",o,re.I)
                if mm:
                    t=int(mm.group(1))
        except: pass
        r.append((dev,t))
    _disk_cache["t"]=now; _disk_cache["v"]=r
    return r

_fan_cache={"t":0,"v":0}
def fan():
    """风扇转速（RPM），来自宿主机 state.json"""
    try:
        return int(hstate().get("fan") or 0)
    except Exception:
        return 0

def uptime():
    s=float(open("/tmp/_proc_uptime").read().split()[0])
    dd,hh=int(s//86400),int((s%86400)//3600)
    if dd:
        return f"{dd}天{hh}时"
    m2=int((s%3600)//60)
    return (f"{hh}时{m2}分" if hh else f"{m2}分")

_net_cache={"t":0,"rx":0,"tx":0,"rr":0.0,"tr":0.0}
# ---- 网络速率历史（30 秒窗口，0.1 秒/点 = 300 点）----
_net_hist = {"rx": [], "tx": [], "t": 0.0}
_NET_WIN = 100         # 曲线窗口点数（0.3 秒/点 = 30 秒）

def net_hist_record(rx, tx):
    """每帧调用；0.3 秒记录一点，30 秒 = 100 点"""
    now = time.time()
    h = _net_hist
    if now - h["t"] < 0.28:     # 0.3 秒/点，100 点正好 30 秒
        return
    h["t"] = now
    h["rx"].append(max(0.0, rx))
    h["tx"].append(max(0.0, tx))
    if len(h["rx"]) > _NET_WIN:
        del h["rx"][:-_NET_WIN]
        del h["tx"][:-_NET_WIN]

def net_hist():
    return _net_hist["rx"], _net_hist["tx"]

# 读数显示锁存：曲线走实时值，屏幕上的数字每秒才换一次
_net_disp = {"t": 0.0, "rx": 0.0, "tx": 0.0, "live": (0.0, 0.0)}

def net_rate():
    """返回实时速率（供曲线用，毫秒级精度）"""
    now=time.time(); iface="br0"
    try:
        rx=int(open(f"/sys/class/net/{iface}/statistics/rx_bytes").read())
        tx=int(open(f"/sys/class/net/{iface}/statistics/tx_bytes").read())
    except: return (0.0,0.0)
    c=_net_cache
    if c["t"]>0 and now-c["t"]>0.03:
        dt=now-c["t"]
        c["rr"]=(rx-c["rx"])/dt/1048576
        c["tr"]=(tx-c["tx"])/dt/1048576
    c["t"]=now; c["rx"]=rx; c["tx"]=tx
    live=(max(0,c["rr"]), max(0,c["tr"]))
    d=_net_disp
    d["live"]=live
    # 每 1 秒才更新一次显示值，数字不跳
    if now - d["t"] >= 1.0:
        d["t"]=now; d["rx"]=live[0]; d["tx"]=live[1]
    return live

def net_display():
    """返回屏幕上显示的速率（每秒锁存一次）"""
    d=_net_disp
    return (d["rx"], d["tx"])

_df_cache={"t":0,"v":{}}
def storage():
    """返回 {"cache": (pct, used_GB, size_GB), "array": (...)}"""
    st = hstate().get("storage") or {}
    out = {}
    for k in ("cache", "array"):
        v = st.get(k)
        if v:
            out[k] = (int(v.get("pct", 0)), float(v.get("used", 0)), float(v.get("size", 0)))
    return out
_cn_cache={"t":0,"v":[]}
def fmt_sec(sec):
    """秒数 -> 中文时长，不含分钟。
    239644 -> "2天18时" ; 84031 -> "23时" ; 不足1时 -> "不足1时"
    """
    d,r=divmod(int(sec),86400)
    h,_=divmod(r,3600)
    if d>0:
        return f"{d}天{h}时" if h else f"{d}天"
    if h>0:
        return f"{h}时"
    return "不足1时"


def cn_num(n):
    """数字转中文（用于季集）：1->1, 10->10, 21->21 保持阿拉伯数字更易读"""
    try:
        return str(int(n))
    except Exception:
        return "?"

def cn_duration(s):
    """把 docker 的 Up 时长换算成中文，自动进位到天/时/分。
    例：43 hours -> 1天19时   |   2 days 4 hours -> 2天4时   |   25 hours -> 1天1时
    """
    s=s.strip()
    if not s: return ""
    if "Less than a second" in s: return "刚刚"
    if "About an hour" in s:     return "约1时"
    if "About a minute" in s:    return "约1分"

    # 抽出所有 "数字 单位"
    units={"second":1,"minute":60,"hour":3600,"day":86400,
           "week":604800,"month":2592000,"year":31536000}
    total=0; found=False
    for num,unit in re.findall(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?",s):
        total += int(num)*units[unit]; found=True
    if not found: return s.replace(" ago","").replace(",","")

    if total < 60:
        return f"{total}秒"
    if total < 3600:
        m_,s_=divmod(total,60)
        return f"{m_}分{s_}秒" if s_ else f"{m_}分"

    d_,rem = divmod(total,86400)
    h_,rem = divmod(rem,3600)
    mi,_   = divmod(rem,60)

    if d_ > 0:
        # 1天以上：显示 天+时，时数为 0 时省略
        return f"{d_}天{h_}时" if h_ else f"{d_}天"
    else:
        # 不足1天：显示 时+分，分钟为 0 时省略
        return f"{h_}时{mi}分" if mi else f"{h_}时"


def containers():
    """返回 [(名称, 是否运行, 启动时间戳)]"""
    d = hstate().get("containers") or []
    out = []
    import datetime
    for c in d:
        run = bool(c.get("running"))
        stamp = 0
        raw = c.get("started") or ""
        if run and raw:
            try:
                t = raw.split(".")[0].replace("Z", "")
                dt = datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")
                stamp = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
            except Exception:
                stamp = 0
        out.append((c.get("name", "?"), run, stamp))
    out.sort(key=lambda x: x[0].lower())
    return out

_dio_cache={"t":0,"prev":{},"rate":{}}
def disk_io():
    """返回 {dev: (read_MBps, write_MBps)}  —— 每块盘实时读写速率
    优先用宿主机算好的 io_rate（8秒窗口，准确）；否则退回本机差值。"""
    _hr = (hstate().get("io_rate") or {})
    if _hr:
        out = {}
        for k, v in _hr.items():
            if not k.startswith(("sd", "nvme")): continue
            if k.startswith("sd") and len(k) != 3: continue
            if k.startswith("nvme") and "p" in k: continue
            out[k] = (float(v[0]), float(v[1]))
        if out:
            return out
    now=time.time()
    cur={}
    _sync_proc()
    try:
        src = "/tmp/_proc_diskstats" if os.path.exists("/tmp/_proc_diskstats") else "/proc/diskstats"
        for l in open(src):
            f=l.split()
            if len(f)<14: continue
            dev=f[2]
            if dev not in ("sdc","sdd","sde"): continue
            # f[5]=读扇区数, f[9]=写扇区数（1扇区=512B）
            cur[dev]=(int(f[5]), int(f[9]))
    except: return {}
    c=_dio_cache
    out={}
    if c["t"]>0 and now-c["t"]>0.5:
        dt=now-c["t"]
        for dev,(rs,ws) in cur.items():
            if dev in c["prev"]:
                prs,pws = c["prev"][dev]
                rd = max(0,(rs-prs))*512/dt/1048576
                wr = max(0,(ws-pws))*512/dt/1048576
                c["rate"][dev]=(rd,wr)
    c["t"]=now; c["prev"]=cur
    for dev in cur:
        out[dev]=c["rate"].get(dev,(0.0,0.0))
    return out

# ---- 镜像数：事件驱动刷新 ----
# 平时 CPU 0%，只有镜像发生变化(docker events)才重算一次；
# 另加 24 小时兜底，防止事件监听意外断掉后永久不更新。
_img_cache={"t":0,"v":(0,0.0,0),"dirty":True}
_img_proc={"p":None}          # events 子进程句柄，退出时一并清理
_IMG_FALLBACK=86400   # 24 小时兜底

def _img_event_watch():
    """后台线程：监听 docker events 的镜像事件，有变化就置 dirty"""
    while True:
        try:
            p=subprocess.Popen(
                ["docker","events","--filter","type=image",
                 "--format","{{.Action}}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
            _img_proc["p"]=p
            # 关心的事件：拉取/删除/打标签/构建/导入
            for line in p.stdout:
                a=line.strip()
                if a in ("pull","delete","untag","tag","build","import","load","prune","save"):
                    _img_cache["dirty"]=True
        except Exception:
            pass
        # 进程掉线，等 10 秒重连
        time.sleep(10)

def _img_start_watcher():
    import threading
    t=threading.Thread(target=_img_event_watch,daemon=True)
    t.start()

def images():
    """返回 (镜像数, 总容量GB, 可回收GB)，来自宿主机 state.json"""
    st = hstate()
    try:
        cnt = int(st.get("img_count") or 0)
        gb  = float(st.get("img_gb") or 0)
        rec = float(st.get("img_reclaim_gb") or 0)
        return cnt, gb, rec
    except Exception:
        return 0, 0.0, 0.0

def parse_size(s):
    s=s.strip()
    m=re.match(r"([\d.]+)\s*([KMGT]?)B?",s)
    if not m: return 0.0
    v=float(m.group(1)); u=m.group(2)
    return v*{"":1/1073741824,"K":1/1048576,"M":1/1024,"G":1,"T":1024}.get(u,1)

def parse_size_mb(s):
    s=s.strip()
    m=re.match(r"([\d.]+)\s*([KMGT]?)B?",s)
    if not m: return 0
    v=float(m.group(1)); u=m.group(2)
    return int(v*{"":1/1048576,"K":1/1024,"M":1,"G":1024,"T":1048576}.get(u,1))


_dd_cache={"t":0,"v":{}}

# ==================== 新增：阵列健康 + Emby 播放 ====================
_par_cache={"t":0,"v":None}
def parity_info():
    """返回 (last_date, last_speed_mbs, errors, next_txt, live)
    live = None 或 dict(pct, speed, elapsed, eta, action)
    """
    now=time.time()
    # 校验进行中时 1 秒刷新（实时），否则 60 秒
    if _par_cache["v"] is not None:
        live_prev = _par_cache["v"][4]
        ttl = 1 if live_prev else 60
        if now-_par_cache["t"]<ttl: return _par_cache["v"]

    last_date=""; speed=0.0; errs=0
    try:
        _pl = hstate().get("parity_log") or ""
        if not _pl:
            try: _pl = open("/boot/config/parity-checks.log").read()
            except Exception: _pl = ""
        lines=[l for l in _pl.strip().splitlines() if l.strip()]
        if lines:
            f=lines[-1].split("|")
            if len(f)>=4:
                try:
                    dt=time.strptime(f[0].strip(),"%Y %b %d %H:%M:%S")
                    last_date=time.strftime("%m-%d %H:%M",dt)
                except: last_date=f[0].strip()[:11]
                try: speed=float(f[2])/1024.0/1024.0
                except: speed=0.0
                try: errs=int(f[3])
                except: errs=0
    except: pass

    # ---------- 实时校验状态 ----------
    live=None
    try:
        mdct = hstate().get("md") or {}
        def g(k):
            v = mdct.get(k)
            return str(v) if v is not None else None
        act=g("mdResyncAction") or ""
        pos=g("mdResyncPos"); size=g("mdResyncSize")
        dt_=g("mdResyncDt"); db=g("mdResyncDb")
        corr=g("mdResyncCorr")
        # 只有 check/recon/sync 且 pos<size 才算进行中
        if ("check" in act or "recon" in act or "sync" in act) and pos and size:
            try:
                p_,s_=int(pos),int(size)
                # pos 必须 > 0 且 < size 才算真正在跑（Unraid 空闲时也会残留 action=check P）
                if s_>0 and 0 < p_ < s_:
                    pct=p_*100//s_
                    # 速度：mdResyncDt = 本周期耗时(毫秒)，mdResyncDb = 本周期数据量(字节)
                    # 实测交叉验证：dt=30, db=3625624 -> 115.2 MB/s，与位置差算出的
                    # 118.2 MB/s 吻合。（原实现把两者弄反，恒显示 0.0 MB/s）
                    spd=0.0
                    if dt_ and db and int(dt_)>0 and int(db)>0:
                        spd=(int(db)/1048576.0)/(int(dt_)/1000.0)
                    rem=(s_-p_)
                    eta=0
                    if spd>1:
                        # rem 单位 1K 块 -> /1024 转 MB，再除以 MB/s
                        eta=int(rem/1024.0/spd)
                    elapsed=int(g("mdResyncElapsed") or 0)
                    act_zh = "校验中" if "check" in act else ("重建中" if "recon" in act else "同步中")
                    live={"pct":pct,"speed":spd,"eta":eta,"action":act_zh,
                          "errs":int(corr) if corr and corr.lstrip("-").isdigit() else 0}
            except: pass
    except: pass

    # 下次校验计划：读 Unraid 的 parity-check.cron（不是 /etc/cron.d/root）
    # 容器内没有 /etc/cron.d，故由 state.json 传入原文
    nxt = ""
    _cron = hstate().get("parity_cron") or ""
    if not _cron:
        try: _cron = open("/boot/config/plugins/dynamix/parity-check.cron").read()
        except Exception: _cron = ""
    for l in _cron.splitlines():
        if "mdcmd check" not in l or l.strip().startswith("#"):
            continue
        parts = l.split()
        if len(parts) < 5:
            continue
        mi, hh, dd, mo, dw = parts[0], parts[1], parts[2], parts[3], parts[4]
        try:
            HH = int(hh); MM = int(mi)
        except Exception:
            HH = 0; MM = 0
        _t = f"{HH:02d}:{MM:02d}"
        wd = {"0":"周日","1":"周一","2":"周二","3":"周三","4":"周四","5":"周五","6":"周六","7":"周日"}
        if dw != "*":
            # 周几（可能是 1,3,5 列表）
            ds = "/".join(wd.get(x, x) for x in dw.split(","))
            nxt = f"每{ds} {_t}"
        elif dd != "*" and mo != "*":
            try: nxt = f"{int(mo):02d} 月 {int(dd):02d} 日 {_t}"
            except Exception: nxt = f"每月 {dd} 日 {_t}"
        elif dd != "*":
            try: DD = int(dd)
            except Exception: DD = 1
            nxt = "每月 1 日 " + _t if DD == 1 else f"每月 {DD} 日 {_t}"
        elif mo != "*":
            nxt = f"每年 {int(mo):02d} 月 {_t}"
        else:
            nxt = f"每天 {_t}"
        break
    if not nxt: nxt="未设置"
    v=(last_date,speed,errs,nxt,live)
    _par_cache["t"]=now; _par_cache["v"]=v
    return v

_raw_host = os.environ.get("_EMBY_HOST") or os.environ.get("EMBY_HOST") or "127.0.0.1"
# 兼容带/不带协议、带/不带端口的写法，统一成 base URL
_EMBY_BASE = _raw_host.strip().rstrip("/")
if not _EMBY_BASE.startswith("http"):
    _EMBY_BASE = "http://" + _EMBY_BASE
if not re.search(r":\d+$", _EMBY_BASE):
    _EMBY_BASE += ":8097"
_EMBY_HOST = _EMBY_BASE.split("//",1)[-1].split(":")[0]
_eb_cache={"t":0,"v":None}
_src_cache = {}          # item_id -> (ts, [MediaSource, ...])


def _item_sources(token, item_id, user_id, ttl=300):
    """取某个 item 的全部 MediaSources（带 5 分钟缓存）。

    会话接口对 mpegts 只回损坏的时长，但 item 详情接口能拿到同一影片
    其它源（如 mkv）的正常 RunTimeTicks。
    """
    if not token or not item_id:
        return []
    now = time.time()
    hit = _src_cache.get(item_id)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        import json as _j, urllib.request as _u
        url = f"{_EMBY_BASE}/emby/Users/{user_id}/Items/{item_id}?api_key={token}"
        raw = _u.urlopen(_u.Request(url, headers={"User-Agent": "unRAID-FP"}),
                         timeout=6).read().decode("utf-8", "ignore")
        d = _j.loads(raw) if raw.strip() else {}
        ms = d.get("MediaSources") or []
    except Exception:
        ms = []
    _src_cache[item_id] = (now, ms)
    return ms


def _pick_runtime(np, pos, extra=None):
    """从 item 及其 MediaSources 中挑出一个可信的时长(RunTimeTicks)。

    mpegts/ts 源的 RunTimeTicks 常常损坏（例如 37120330 = 3.7 秒，
    而实际两小时），直接参与计算会得到 1000%+ 的进度。
    这里收集所有候选时长，剔除不可能的值后取最合理的一个。
    """
    cands = []
    v = np.get("RunTimeTicks")
    if v:
        cands.append(v)
    for ms in (np.get("MediaSources") or []):
        v = ms.get("RunTimeTicks")
        if v:
            cands.append(v)
    for ms in (extra or []):          # 来自 item 详情的补充源
        v = ms.get("RunTimeTicks")
        if v:
            cands.append(v)

    # 合法区间：10 分钟 ~ 24 小时（单位 100ns）
    lo, hi = 10 * 60 * 10000000, 24 * 3600 * 10000000
    ok = [c for c in cands if lo <= c <= hi]
    if not ok:
        return 0

    # 优先取能容纳当前位置的时长里最小的那个（最贴近真实值），
    # 若都不够大，则取最大的合法值
    fit = [c for c in ok if c >= pos]
    if fit:
        return min(fit)
    return max(ok)


def emby_all():
    """返回 [(title, sub, pct, paused, client, username), ...] 或 None（无播放）
    支持多路并发播放"""
    now=time.time()
    if now-_eb_cache["t"]<3: return _eb_cache["v"]
    _all=[]
    try:
        import json as _json
        # 从 authentication.db 取一个活跃 token（缓存）
        global _eb_token, _tks_full, _tks_ts
        try:
            tk=_eb_token
        except NameError:
            tk=None
        # 取所有活跃 token（可能有很多历史残留）。
        # 必须每次都拿全量：不同客户端用不同 token 登录，
        # 各自能看到的会话集合不同，只用一个 token 会漏掉别的播放会话。
        if not _tks_full or now - _tks_ts > 30:
            import sqlite3
            _db = os.environ.get("EMBY_DB") or "/emby-data/authentication.db"
            c=sqlite3.connect(f"file:{_db}?mode=ro",uri=True,timeout=3)
            cur=c.cursor()
            cur.execute("SELECT AccessToken FROM Tokens_2 WHERE IsActive=1 ORDER BY Id DESC")
            _tks_full=[r[0] for r in cur.fetchall() if r and r[0]]
            c.close()
            _tks_ts = now
        _tks = _tks_full
        tk = tk or (_tks[0] if _tks else None)
        if tk:

            import urllib.request as _ur
            # 不同 token 可见的会话集合不同（每个客户端用各自的 token 登录），
            # 只取第一个有播放的 token 会漏掉另一路正在播放的会话。
            # 因此遍历所有活跃 token，把会话按 Id 合并去重。
            _merged = {}
            _any_play = False
            for _t in _tks[:12]:
                try:
                    _rq = _ur.Request(
                        f"{_EMBY_BASE}/emby/Sessions?api_key={_t}",
                        headers={"User-Agent": "unRAID-FP"})
                    _o = _ur.urlopen(_rq, timeout=6).read().decode("utf-8", "ignore")
                    _se = _json.loads(_o) if _o.strip() else []
                except Exception:
                    continue
                if not isinstance(_se, list):
                    continue
                for _x in _se:
                    if not isinstance(_x, dict):
                        continue
                    _sid = _x.get("Id") or _x.get("DeviceId")
                    if not _sid:
                        continue
                    # 同一会话被多个 token 看到时，优先保留带 NowPlayingItem 的
                    _prev = _merged.get(_sid)
                    if _prev is None or (not _prev.get("NowPlayingItem")
                                         and _x.get("NowPlayingItem")):
                        _merged[_sid] = _x
                    if _x.get("NowPlayingItem"):
                        _any_play = True
                        _eb_token = _t
            sess = list(_merged.values())
            if not tk:
                sess = []
            _all = []
            for s in sess:
                np=s.get("NowPlayingItem")
                if not np: continue
                ps=s.get("PlayState",{}) or {}
                pos=ps.get("PositionTicks",0) or 0
                dur=_pick_runtime(np, pos)      # ts 源的时长需纠正
                if not dur and np.get("Id"):
                    # mpegts 源时长损坏且会话里没有别的源，去 item 详情补
                    _tk2 = _eb_token if _eb_token else tk
                    _ms = _item_sources(_tk2, np.get("Id"), s.get("UserId"))
                    dur = _pick_runtime(np, pos, _ms)
                pct=int(pos/dur*100) if dur else 0
                pct=max(0, min(100, pct))       # 夹紧，杜绝 1000%+
                paused=bool(ps.get("IsPaused"))
                client=(s.get("Client","") or "")[:10]
                # 主标题
                typ = np.get("Type","") or ""
                if typ == "Episode":
                    title=np.get("SeriesName","") or np.get("Name","")
                    sn=np.get("ParentIndexNumber"); en=np.get("IndexNumber")
                    ep_name = (np.get("Name","") or "").strip()
                    # 剧集名若和剧名重复则不再附加
                    if ep_name == title:
                        ep_name = ""
                    if sn is not None and en is not None:
                        sub=f"第{cn_num(sn)}季第{cn_num(en)}集"
                        if ep_name:
                            sub += f" - {ep_name}"
                    else:
                        sub = ep_name or "剧集"
                elif typ == "Movie":
                    title=np.get("Name","")
                    sub="电影"
                elif typ == "Audio":
                    title=np.get("Name","")
                    sub="音乐"
                elif typ in ("TvChannel","LiveTvChannel"):
                    title=np.get("Name","")
                    sub="直播"
                elif typ == "MusicVideo":
                    title=np.get("Name","")
                    sub="音乐视频"
                elif typ == "Trailer":
                    title=np.get("Name","")
                    sub="预告片"
                elif typ == "Video":
                    title=np.get("Name","")
                    sub="视频"
                elif typ == "Book":
                    title=np.get("Name","")
                    sub="书籍"
                else:
                    title=np.get("Name","") or "未知"
                    sub=""
                uname=(s.get("UserName","") or "").strip()
                # --- 海报信息：电影用封面，剧集用当前集的图 ---
                _img = None
                try:
                    _tags = np.get("ImageTags") or {}
                    if typ == "Movie":
                        if _tags.get("Primary"):
                            _img = {"kind": "Primary", "id": np.get("Id")}
                    elif typ == "Episode":
                        # 优先当前集的 Primary（该集自己的截图/海报）
                        if _tags.get("Primary"):
                            _img = {"kind": "Primary", "id": np.get("Id")}
                        else:
                            # 回落：所属剧集的横版 Backdrop，再回落 Thumb/Primary
                            _sid = np.get("SeriesId")
                            if _sid:
                                _img = {"kind": "Backdrop", "id": _sid}
                    else:
                        if _tags.get("Primary"):
                            _img = {"kind": "Primary", "id": np.get("Id")}
                except Exception:
                    _img = None
                _all.append((title,sub,pct,paused,client,uname,_img))
    except Exception as _e:
        # 不再静默吞异常，写入日志便于排查
        try:
            import traceback as _tb
            open('/app/logs/emby.log','a').write(
                time.strftime('%F %T')+' '+repr(_e)+'\n'+_tb.format_exc()+'\n')
        except Exception: pass
    _eb_cache["t"]=now; _eb_cache["v"]=(_all if _all else None)
    return (_all if _all else None)
_tks_full=[]
_tks_ts=0
_st_cache={"t":0,"v":[]}
_vm_cache={"t":0,"v":[]}
def vms():
    """返回 [(名称, 是否运行)]"""
    d = hstate().get("vms") or []
    return [(v.get("name", "?"), bool(v.get("running"))) for v in d]

# ==================== 海报墙轮播（数据源 Emby） ====================
_poster = {
    "items": [],        # [(海报bytes, 标题)]
    "t": 0.0,           # 上次抓取时间
    "idx": 0,           # 当前显示第几张
    "last": 0.0,        # 上次切换时间
    "lock": False,
    "cur": None,        # 当前选中的 (bytes, 标题)
    "picked": 0.0,      # 选中时间
}
_POSTER_TTL   = 86400    # 海报列表 24 小时刷新一次
_POSTER_SWAP  = 86400    # 24 小时轮换一张（静态显示，不滚动）
_POSTER_MAX   = 24       # 缓存最多 24 张，供每日轮换
_POSTER_PICK  = "/app/logs/poster_pick.json"   # 记录当前选中海报与切换时间

def _poster_fetch():
    """从 Emby 取最近添加的影片海报（阻塞，调用方需加锁）"""
    import json as _j, urllib.request as _u, sqlite3
    db = os.environ.get("EMBY_DB") or "/emby-data/authentication.db"
    host = _EMBY_BASE
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        tks = [r[0] for r in c.execute(
            "SELECT AccessToken FROM Tokens_2 WHERE IsActive=1 ORDER BY Id DESC"
        ).fetchall() if r and r[0]]
        c.close()
    except Exception:
        return []
    items = []
    for t in tks[:6]:
        try:
            u = (f"{host}/emby/Items?Recursive=true&IncludeItemTypes=Movie,Series"
                 f"&Limit=24&SortBy=DateCreated&SortOrder=Descending"
                 f"&Fields=ProductionYear&api_key={t}")
            o = _j.loads(_u.urlopen(u, timeout=10).read())
            lst = o.get("Items") or []
            if not lst:
                continue
            for it in lst[:_POSTER_MAX]:
                if not (it.get("ImageTags") or {}).get("Primary"):
                    continue
                try:
                    pu = (f"{host}/emby/Items/{it['Id']}/Images/Primary"
                          f"?maxHeight=260&quality=85&api_key={t}")
                    raw = _u.urlopen(pu, timeout=12).read()
                    if len(raw) > 800:
                        items.append((raw, it.get("Name", "")))
                except Exception:
                    continue
                if len(items) >= _POSTER_MAX:
                    break
            if items:
                break
        except Exception:
            continue
    return items

def _poster_worker(log=None):
    """后台线程：定时刷新海报列表，不阻塞主渲染。
    首次抓取失败时每 60 秒重试（Emby 可能还没就绪）；成功后再按 24 小时周期。"""
    _ok = False
    while True:
        try:
            if not _poster["lock"]:
                it = _poster_fetch()
                if it:
                    _poster["lock"] = True
                    _poster["items"] = it
                    _poster["t"] = time.time()
                    if _poster["idx"] >= len(it):
                        _poster["idx"] = 0
                    _poster["lock"] = False
                    _ok = True
                    if log:
                        try:
                            log.write(f"poster ok: {len(it)} @ {time.strftime('%T')}\n")
                        except Exception: pass
                else:
                    # 抓空：保持旧的 items 不动，稍后重试
                    if log:
                        try:
                            log.write(f"poster empty @ {time.strftime('%T')}\n")
                        except Exception: pass
        except Exception:
            _poster["lock"] = False
            if log:
                try: log.write(f"poster err {time.strftime('%T')}\n")
                except Exception: pass
        time.sleep(_POSTER_TTL if _ok else 60)

def _poster_start(log=None):
    import threading
    t = threading.Thread(target=_poster_worker, args=(log,), daemon=True)
    t.start()
    return t

def _poster_load_pick():
    """启动时恢复上次选中的海报（索引 + 时间），避免重启就换图"""
    import json as _j
    try:
        with open(_POSTER_PICK, "r", encoding="utf-8") as f:
            o = _j.load(f)
        return int(o.get("idx", 0)), float(o.get("picked", 0.0))
    except Exception:
        return None

def _poster_save_pick(idx, picked):
    import json as _j
    try:
        os.makedirs(os.path.dirname(_POSTER_PICK), exist_ok=True)
        with open(_POSTER_PICK, "w", encoding="utf-8") as f:
            _j.dump({"idx": idx, "picked": picked}, f)
    except Exception:
        pass

# ===== 播放中内容的横版海报（右侧区域）=====
# 缓存结构：{(kind,item_id): (ts, bytes, PIL光图)}，24 小时 TTL
_playimg = {"cache": {}, "lock": False, "warned": False}
_PLAYIMG_TTL = 86400
_playimg_pending = {"keys": [], "lock": None}


def _fetch_play_image(kind, item_id, token):
    """按图片类型取图。Backdrop=横版剧照，Primary=竖版封面。"""
    import urllib.request as _u
    host = _EMBY_BASE
    # 横版目标 382x206 区域，取稍大一点保证清晰
    if kind == "Backdrop":
        idx = 0
        url = (f"{host}/emby/Items/{item_id}/Images/Backdrop/{idx}"
               f"?maxWidth=520&quality=88&api_key={token}")
    else:
        url = (f"{host}/emby/Items/{item_id}/Images/Primary"
               f"?maxWidth=520&quality=88&api_key={token}")
    try:
        raw = _u.urlopen(url, timeout=12).read()
        if len(raw) > 1200:
            return raw
    except Exception:
        pass
    # 回落：Backdrop 拿不到时试 Primary，反之亦然
    try:
        alt = ("Primary" if kind == "Backdrop" else "Backdrop")
        if alt == "Backdrop":
            url2 = (f"{host}/emby/Items/{item_id}/Images/Backdrop/0"
                    f"?maxWidth=520&quality=88&api_key={token}")
        else:
            url2 = (f"{host}/emby/Items/{item_id}/Images/Primary"
                    f"?maxWidth=520&quality=88&api_key={token}")
        raw = _u.urlopen(url2, timeout=12).read()
        if len(raw) > 1200:
            return raw
    except Exception:
        pass
    return None


def _playimg_worker(log=None):
    """后台线程：按当前播放项的 (kind,id) 拉取海报，缓存 24h。"""
    while True:
        try:
            _q = _playimg_pending["keys"]
            if not _q:
                time.sleep(0.5)
                continue
            kind, item_id = _q.pop(0)
            ck = (kind, item_id)
            now = time.time()
            hit = _playimg["cache"].get(ck)
            if hit and now - hit[0] < _PLAYIMG_TTL:
                continue
            # 取 token
            token = None
            try:
                token = _eb_token
            except Exception:
                token = None
            if not token:
                try:
                    import sqlite3
                    db = os.environ.get("EMBY_DB") or "/emby-data/authentication.db"
                    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                    r = c.execute("SELECT AccessToken FROM Tokens_2 WHERE IsActive=1 "
                                  "ORDER BY Id DESC").fetchone()
                    c.close()
                    token = r[0] if r else None
                except Exception:
                    token = None
            if not token:
                time.sleep(5.0)
                continue
            raw = _fetch_play_image(kind, item_id, token)
            if raw:
                _playimg["cache"][ck] = (now, raw)
                # 只保留最近 12 张，避免长期运行内存膨胀
                if len(_playimg["cache"]) > 12:
                    for k in sorted(_playimg["cache"], key=lambda x: _playimg["cache"][x][0])[:-12]:
                        _playimg["cache"].pop(k, None)
            else:
                _playimg["cache"][ck] = (now, None)
                if log and not _playimg["warned"]:
                    try:
                        log.write(f"playimg miss: {kind}/{item_id}\n")
                    except Exception:
                        pass
                    _playimg["warned"] = True
        except Exception:
            pass
        time.sleep(1.0)


def _playimg_start(log=None):
    import threading
    t = threading.Thread(target=_playimg_worker, args=(log,), daemon=True)
    t.start()
    return t


def play_image(kind, item_id):
    """取缓存的图片字节；未命中时排队给后台线程。"""
    if not kind or not item_id:
        return None
    ck = (kind, item_id)
    now_t = time.time()
    hit = _playimg["cache"].get(ck)
    if hit and now_t - hit[0] < _PLAYIMG_TTL:
        return hit[1]
    _q = _playimg_pending["keys"]
    if ck not in _q:
        _q.append(ck)
        if len(_q) > 8:
            del _q[:-8]
    return hit[1] if hit else None


def poster_current():
    """静态海报：24 小时才换一张；无列表时返回 None"""
    st = _poster
    lst = st["items"]
    if not lst:
        return None
    now = time.time()

    # 首次：尝试恢复落盘记录
    if st["picked"] == 0.0:
        rec = _poster_load_pick()
        if rec:
            idx, picked = rec
            st["idx"] = idx % len(lst)
            st["picked"] = picked
        else:
            st["idx"] = 0
            st["picked"] = now
            _poster_save_pick(0, now)

    # 到期轮换
    if now - st["picked"] >= _POSTER_SWAP:
        st["idx"] = (st["idx"] + 1) % len(lst)
        st["picked"] = now
        _poster_save_pick(st["idx"], now)

    st["cur"] = lst[st["idx"] % len(lst)]
    return st["cur"]

# ==================== 绘制 ====================
def C(r,g,b): return (max(0,min(255,int(r))),max(0,min(255,int(g))),max(0,min(255,int(b))))
GRN=C(*_LAYOUT["color_green"]); ORG=C(*_LAYOUT["color_orange"]); RED=C(*_LAYOUT["color_red"])
DIM=C(*_LAYOUT["color_dim"]); GRY=C(*_LAYOUT["color_gray"]); BLU=C(*_LAYOUT["color_blue"]); CYA=C(*_LAYOUT["color_cyan"])


def draw_ring(d, cx, cy, r, pct, color, track=C(44,56,80), w=5):
    """在 (cx,cy) 画半径 r 的环形进度条，pct=0..100"""
    d.ellipse([cx-r,cy-r,cx+r,cy+r], outline=track, width=w)
    if pct<=0: return
    ext = 360.0*min(pct,100)/100.0
    d.arc([cx-r,cy-r,cx+r,cy+r], start=-90, end=-90+ext, fill=color, width=w)

def fmt_rate(mb):
    if mb>=1.0: return f"{mb:.1f} MB/s"
    kb=mb*1024
    if kb>=10: return f"{kb:.0f} KB/s"
    if kb>=1: return f"{kb:.1f} KB/s"
    return f"{mb*1048576:.0f} B/s"

def render():
    from PIL import Image, ImageDraw, ImageFont
    # 字体对象按字号缓存 —— 避免每帧成百上千次重新解析 16.8MB 字库
    _fcache = {}
    def F(s):
        k = ('latin', s)
        if k not in _fcache:
            _fcache[k] = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", s)
        return _fcache[k]
    def FZ(s):
        """中文字体（文泉驿正黑），缺失则回退 DejaVu"""
        k = ('zh', s)
        if k not in _fcache:
            try:
                _fcache[k] = ImageFont.truetype("/app/fonts/wqy-zenhei.ttc", s)
            except Exception:
                _fcache[k] = F(s)
        return _fcache[k]

    img = Image.new("RGB",(VW,VH),tuple(_LAYOUT["color_bg"]))   # 主背景（深夜蓝 B）
    d = ImageDraw.Draw(img)
    _twcache = {}
    def TW(t,f):
        k = (t, id(f))
        r = _twcache.get(k)
        if r is None:
            b=d.textbbox((0,0),t,font=f); r=b[2]-b[0]
            if len(_twcache) > 4000: _twcache.clear()
            _twcache[k] = r
        return r

    ct,cavg = cpu_temp()
    cu = cpu_usage()
    ld = load()
    mtot,mused = meminfo()
    st,prog = mdstate()
    dk = disks()
    fn = fan()
    up = uptime()
    rxr,txr = net_rate()
    net_hist_record(rxr, txr)
    sto = storage()
    cn = containers()
    vm = vms()
    icnt,igb,irec = images()
    dio = disk_io()
    par = parity_info()
    eb = emby_all()

    d.rectangle([0,0,VW-1,VH-1], outline=C(40,160,240), width=3)

    # ===== 头部 =====
    d.rectangle([3,3,VW-4,86], fill=C(22,32,54))
    d.text((18,8), "unRAID", font=F(34), fill=CYA)
    d.text((18,50), "LLLpx", font=F(21), fill=C(145,165,195))
    tnow=time.strftime("%H:%M:%S")
    ft=F(40)
    d.text((VW-20-TW(tnow,ft),6), tnow, font=ft, fill=C(150,200,240))
    dm=time.strftime("%m-%d")
    d.text((VW-20-TW(dm,F(17)),56), dm, font=F(17), fill=C(155,175,205))

    y=94
    def sep(yy, pad=10):
        d.rectangle([16,yy,VW-16,yy+2], fill=C(50,66,96))
        return yy+pad

    # ===== 系统 =====
    d.text((16,y), "CPU", font=F(21), fill=DIM)
    f_big=F(46); f_c=F(20)
    s_temp=str(ct)
    tx=64
    d.text((tx,y-14), s_temp, font=f_big, fill=ORG)
    tx += TW(s_temp,f_big)+2
    d.text((tx,y+4), "\u00b0C", font=f_c, fill=ORG)
    tx += TW("\u00b0C",f_c)+12    # 动态：温度块之后留间隔
    # CPU 型号（位置随温度宽度动态计算）
    mdl = cpu_model()
    d.text((tx,y-3), mdl, font=F(21), fill=C(140,158,185))
    _fs = FZ(15)
    _avg_txt = f"均温 {cavg}\u00b0C"
    d.text((tx,y+27), _avg_txt, font=_fs, fill=DIM)
    # 实时功耗：紧跟「均温」后面同一行，字号一致
    _pw = cpu_power()
    if _pw is not None:
        _px2 = tx + TW(_avg_txt, _fs) + 14
        d.text((_px2, y+27), f"{_pw:.1f}W", font=_fs, fill=DIM)

    # ===== 两个环形：CPU% 在上, MEM% 在下（竖排，靠左居中）=====
    mem_pct = mused*100//mtot
    cuc = GRN if cu<70 else (ORG if cu<90 else RED)
    cmc = GRN if mem_pct<70 else (ORG if mem_pct<90 else RED)
    R=27; W2=7                  # 圆环放大（原 23/6）
    cy1 = y+22                  # 圆心位置保持原样，不撑开整栏
    cx1 = VW-16-250             # CPU 环
    cx2 = VW-16-158             # MEM 环

    # CPU 环（横排左）
    draw_ring(d, cx1, cy1, R, cu, cuc, w=W2)
    t1=f"{cu:.0f}%"
    d.text((cx1-TW(t1,F(17))//2, cy1-9), t1, font=F(17), fill=cuc)
    d.text((cx1-TW("CPU",F(16))//2, cy1+29), "CPU", font=F(16), fill=DIM)  # CPU 保留英文

    # MEM 环（横排右）
    draw_ring(d, cx2, cy1, R, mem_pct, cmc, w=W2)
    t2=f"{mem_pct}%"
    d.text((cx2-TW(t2,F(17))//2, cy1-9), t2, font=F(17), fill=cmc)
    d.text((cx2-TW("内存",FZ(16))//2, cy1+29), "内存", font=FZ(16), fill=DIM)
    y += 54

    d.text((16,y), "负载", font=FZ(19), fill=DIM)
    d.text((72,y-2), "  ".join(ld), font=F(21), fill=GRY)
    up_txt = f"已运行 {up}"
    d.text((VW-18-TW(up_txt,FZ(17)), y+1), up_txt, font=FZ(17), fill=DIM)
    y += 28

    d.text((16,y), "内存", font=FZ(19), fill=DIM)
    pct = mused*100//mtot
    f1,f2,f3=F(26),F(19),F(18)
    x=72
    s1=f"{mused/1048576:.1f}"
    d.text((x,y-3), s1, font=f1, fill=GRY); x+=TW(s1,f1)+4
    d.text((x,y+2), "/", font=f2, fill=DIM); x+=TW("/",f2)+4
    s3=f"{mtot/1048576:.1f}G"
    d.text((x,y), s3, font=f3, fill=C(172,186,208))
    sp=f"{pct}%"
    d.text((VW-18-TW(sp,F(22)), y-2), sp, font=F(22), fill=BLU)
    y += 30
    d.rectangle([16,y,VW-16,y+14], fill=C(40,50,72))
    pw=int((VW-32)*pct/100)
    if pw>0: d.rectangle([16,y,16+pw,y+14], fill=BLU)
    y += 24

    y = sep(y)

    # ===== 阵列 =====
    d.text((16,y), "阵列", font=FZ(19), fill=DIM)
    ac = GRN if st=="STARTED" else RED
    st_zh = {"STARTED":"已启动","STOPPED":"已停止","NEW_ARRAY":"新阵列"}.get(st, st)
    d.ellipse([80,y+3,93,y+16], fill=ac)
    d.text((100,y-1), st_zh, font=FZ(19), fill=ac)
    fan_pre="风扇 "; fan_num=str(fn)
    f_fp=FZ(17); f_fn=F(19)
    fan_w=TW(fan_pre,f_fp)+TW(fan_num,f_fn)
    fan_x=VW-18-fan_w
    d.text((fan_x,y), fan_pre, font=f_fp, fill=C(150,160,200))
    d.text((fan_x+TW(fan_pre,f_fp),y-2), fan_num, font=f_fn, fill=C(150,160,200))
    y += 30

    # 磁盘卡片：一行排开，宽度按数量自动均分（避免溢出右侧边框）
    _n = max(1, len(dk))
    _gap = 6
    _total = VW - 36                      # 左右各留 18
    _cw = (_total - _gap*(_n-1)) // _n    # 每张卡宽度
    for i,(n,t) in enumerate(dk):
        xx = 18 + i*(_cw+_gap)
        CH_ = 38
        d.rectangle([xx,y,xx+_cw,y+CH_], fill=C(24,32,50))
        # 磁盘名（中文必须用 FZ，否则方框），垂直居中
        fname=FZ(17)
        nb=d.textbbox((0,0),n,font=fname)
        d.text((xx+7, y+(CH_-(nb[3]-nb[1]))//2-nb[1]), n, font=fname, fill=C(172,186,208))
        # 温度/休眠统一右对齐 + 垂直居中
        RX_ = xx + _cw - 7
        if t is None:
            sl="休眠"; fsl=FZ(19)
            sb=d.textbbox((0,0),sl,font=fsl)
            d.text((RX_-TW(sl,fsl), y+(CH_-(sb[3]-sb[1]))//2-sb[1]), sl, font=fsl, fill=C(140,230,250))
        else:
            tc = GRN if t<40 else (ORG if t<50 else RED)
            fb_=F(21); fdeg=F(13); st_=str(t)
            tb=d.textbbox((0,0),st_,font=fb_)
            ty = y+(CH_-(tb[3]-tb[1]))//2-tb[1]
            w_all = TW(st_,fb_)+2+TW("\u00b0C",fdeg)
            tx_ = RX_-w_all
            d.text((tx_, ty), st_, font=fb_, fill=tc)
            d.text((tx_+TW(st_,fb_)+2, ty+8), "\u00b0C", font=fdeg, fill=tc)
    y += 44
    if prog>0:
        d.text((16,y), f"校验 {prog}%", font=FZ(17), fill=ORG)
        y += 20
        d.rectangle([16,y,VW-16,y+10], fill=C(40,50,72))
        pw=int((VW-32)*prog/100)
        if pw>0: d.rectangle([16,y,16+pw,y+10], fill=ORG)
        y += 16

    y = sep(y)

    # ===== 存储 | 网络（左右各占一半）=====
    _CW = (VW - 32) // 2          # 每栏宽度
    _SL = 16                      # 左栏起点：存储
    _SR = 16 + _CW + 8            # 右栏起点：网络

    # --- 左：存储 ---
    d.text((_SL, y), "存储", font=FZ(19), fill=DIM)
    ic_txt = f"{icnt} 个  {igb:.1f}G"
    d.text((_SL+_CW-6-TW(ic_txt,FZ(15)), y+3), ic_txt, font=FZ(15), fill=C(185,198,220))
    _sy = y + 24
    for key,label in [("cache","缓存盘"),("array","阵列")]:
        if key not in sto: continue
        pc,used,size = sto[key]
        d.text((_SL+2,_sy-2), label, font=FZ(17), fill=DIM)
        d.text((_SL+64,_sy-3), f"{pc}%", font=F(21), fill=BLU if pc<80 else ORG)
        sz=f"{used:.0f}/{size:.0f}G"
        d.text((_SL+_CW-6-TW(sz,F(15)), _sy+1), sz, font=F(15), fill=C(172,186,208))
        _sy += 21
        d.rectangle([_SL+2,_sy,_SL+_CW-6,_sy+9], fill=C(40,50,72))
        pw=int((_CW-8)*pc/100)
        if pw>0: d.rectangle([_SL+2,_sy,_SL+2+pw,_sy+9], fill=BLU if pc<80 else ORG)
        _sy += 17

    # --- 右：网络（标题行含 br0 与读数，下方整块给曲线图）---
    d.text((_SR, y), "网络", font=FZ(19), fill=DIM)
    _nf = FZ(17)
    # br0 左移靠近标题，留 10px 间隙
    _br0_x = _SR + TW("网络", FZ(19)) + 10
    d.text((_br0_x, y+2), "br0", font=_nf, fill=C(165,180,205))

    _rx_rx, _rx_tx = net_hist()
    _txt_r = _SR + _CW - 6                      # 读数字右缘

    # 读数与标题同行，同字号：↑ 上传在右，↓ 下载在其左
    _dra, _dta = net_display()        # 读数每秒才更新，曲线用实时值
    sdn = fmt_rate(_dra)
    sup = fmt_rate(_dta)
    _upw = TW(sup, _nf)
    d.text((_txt_r - _upw, y+2), sup, font=_nf, fill=BLU)
    d.text((_txt_r - _upw - 14, y+2), "\u2191", font=_nf, fill=BLU)
    _dnr = _txt_r - _upw - 14 - 14              # ↓ 读数字右缘
    _dnw = TW(sdn, _nf)
    d.text((_dnr - _dnw, y+2), sdn, font=_nf, fill=GRN)
    d.text((_dnr - _dnw - 14, y+2), "\u2193", font=_nf, fill=GRN)

    # 曲线图：占满标题下方全部剩余高度，底部与存储栏对齐
    _gr_x1 = _SR + 4
    _gr_x2 = _SR + _CW - 6
    _gr_y1 = y + 26
    _gr_y2 = max(_gr_y1 + 40, _sy - 6) if _sy else _gr_y1 + 40

    if _gr_x2 > _gr_x1 + 30:
        d.rectangle([_gr_x1, _gr_y1, _gr_x2, _gr_y2], fill=C(20,28,44))

        _gw = _gr_x2 - _gr_x1 - 4
        _gh = _gr_y2 - _gr_y1 - 4
        _gx0 = _gr_x1 + 2
        _gy0 = _gr_y2 - 2

        # --- 低亮度网格 ---
        _gc = C(34, 46, 66)                      # 网格线颜色（很暗的蓝灰）
        for _i in (1, 2, 3):                     # 3 条横线
            _gy = _gr_y1 + 2 + int(_gh * _i / 4)
            d.line([(_gx0, _gy), (_gx0 + _gw, _gy)], fill=_gc, width=1)
        for _i in (1, 2, 3, 4):                  # 4 条竖线，每格 6 秒（30 秒窗口）
            _gx = _gx0 + int(_gw * _i / 5)
            d.line([(_gx, _gr_y1 + 2), (_gx, _gr_y2 - 2)], fill=_gc, width=1)

        if len(_rx_rx) >= 2:
            vmax = max(max(_rx_rx), max(_rx_tx)) if _rx_rx else 0
            vmax = max(vmax * 1.15, 0.05)

            def _pts(series):
                """采样点 -> 归一化坐标，x 用浮点避免台阶"""
                n = len(series)
                out = []
                for i, v in enumerate(series):
                    x = _gx0 + _gw * i / max(1, _NET_WIN - 1)
                    yy = _gy0 - _gh * min(1.0, v / vmax)
                    out.append((x, yy))
                return out

            def _smooth(pts, steps=2):
                """Catmull-Rom 插值，把折线变成连续曲线"""
                n = len(pts)
                if n < 3:
                    return [(int(x), int(yy)) for x, yy in pts]
                res = []
                for i in range(n - 1):
                    p0 = pts[i - 1] if i > 0 else pts[i]
                    p1 = pts[i]
                    p2 = pts[i + 1]
                    p3 = pts[i + 2] if i + 2 < n else p2
                    for s_ in range(steps):
                        t = s_ / steps
                        t2 = t * t
                        t3 = t2 * t
                        x = 0.5 * ((2*p1[0]) + (-p0[0]+p2[0])*t
                                   + (2*p0[0]-5*p1[0]+4*p2[0]-p3[0])*t2
                                   + (-p0[0]+3*p1[0]-3*p2[0]+p3[0])*t3)
                        yy = 0.5 * ((2*p1[1]) + (-p0[1]+p2[1])*t
                                    + (2*p0[1]-5*p1[1]+4*p2[1]-p3[1])*t2
                                    + (-p0[1]+3*p1[1]-3*p2[1]+p3[1])*t3)
                        # 夹在绘图区内，防插值过冲
                        yy = max(_gr_y1 + 2, min(_gr_y2 - 2, yy))
                        res.append((int(x), int(yy)))
                res.append((int(pts[-1][0]), int(pts[-1][1])))
                return res

            p_tx = _smooth(_pts(_rx_tx))
            p_rx = _smooth(_pts(_rx_rx))
            if len(p_tx) >= 2:
                d.line(p_tx, fill=C(120,205,255), width=1, joint="curve")
            if len(p_rx) >= 2:
                d.line(p_rx, fill=C(90,235,130), width=2, joint="curve")
        # 图例
        _lg = F(12)
        d.text((_gr_x1 + 4, _gr_y1 + 2), "\u2193", font=_lg, fill=C(90,235,130))
        d.text((_gr_x2 - 12, _gr_y1 + 2), "\u2191", font=_lg, fill=C(120,205,255))

    y = max(_sy, _gr_y2 + 4)

    y = sep(y, 8)
    # ===== 磁盘读写 | 校验（左右并列，各占一半）=====
    _COLW = (VW - 32) // 2        # 每栏宽度
    _LX = 16                      # 左栏：磁盘读写
    _RX = 16 + _COLW + 8          # 右栏：校验

    d.text((_LX, y), "磁盘读写", font=FZ(19), fill=DIM)
    d.text((_RX, y), "校验", font=FZ(19), fill=DIM)
    d.line([(_LX+_COLW+3, y+2), (_LX+_COLW+3, y+60)], fill=C(50,66,96), width=1)

    _dy = y + 24
    # 左栏：数据盘竖排两行
    for dev, _zh in [("sdb","缓存盘"),("sdc","硬盘1"),("sde","硬盘2")]:
        rd,wr = dio.get(dev,(0.0,0.0))
        d.rectangle([_LX+2, _dy, _LX+_COLW-6, _dy+22], fill=C(20,28,44))
        d.text((_LX+10, _dy+3), _zh, font=FZ(15), fill=C(215,225,240))
        rc = GRN if rd>0.05 else C(158,172,196)
        wc = BLU if wr>0.05 else C(158,172,196)
        # 固定两列：↓ 列和 ↑ 列各自右对齐，空不空闲都对齐
        _c2 = _LX+_COLW-8          # ↑ 列右缘（卡片右侧留 8px）
        _c1 = _LX+_COLW-6-100      # ↓ 列右缘（距卡片右缘 100px）
        if rd>0:
            rd_v, rdf = fmt_rate(rd), F(14)
        else:
            rd_v, rdf = "空闲", FZ(14)
        if wr>0:
            wr_v, wrf = fmt_rate(wr), F(14)
        else:
            wr_v, wrf = "空闲", FZ(14)
        # 箭头单独画，文字右对齐到各自列，箭头贴在文字左侧 3px
        _rdx = _c1 - TW(rd_v,rdf)
        d.text((_rdx-14, _dy+4), "\u2193", font=F(14), fill=rc)
        d.text((_rdx, _dy+4), rd_v, font=rdf, fill=rc)
        _wrx = _c2 - TW(wr_v,wrf)
        d.text((_wrx-14, _dy+4), "\u2191", font=F(14), fill=wc)
        d.text((_wrx, _dy+4), wr_v, font=wrf, fill=wc)
        _dy += 25

    # 右栏：校验进度 / 上次结果
    pdate, pspeed, perr, pnext, plive = par
    _ry = y + 24
    if plive:
        d.text((_RX+2, _ry), plive["action"], font=FZ(14), fill=ORG)
        _pt = f"{plive['pct']}%"
        _pf = F(17)
        d.text((_RX+_COLW-6-TW(_pt,_pf), _ry-2), _pt, font=_pf, fill=ORG)
        _ry += 21
        d.rectangle([_RX+2, _ry, _RX+_COLW-6, _ry+10], fill=C(40,50,72))
        _fw = int((_COLW-8)*plive["pct"]/100.0)
        if _fw>0: d.rectangle([_RX+2, _ry, _RX+2+_fw, _ry+10], fill=C(255,175,60))
        _ry += 16
        _sp = f"{plive['speed']:.0f}MB/s" if plive["speed"]>0 else "--"
        d.text((_RX+2, _ry), _sp, font=F(14), fill=C(120,205,255))
        if plive["eta"]>0:
            _eh = plive["eta"]//3600; _em = (plive["eta"]%3600)//60
            _et = f"剩{_eh}时{_em}分" if _eh else f"剩{_em}分"
        else:
            _et = "计算中"
        _ef = FZ(14)
        d.text((_RX+_COLW-6-TW(_et,_ef), _ry), _et, font=_ef, fill=C(195,208,228))
        _ry += 20
    else:
        d.text((_RX+2, _ry), "上次", font=FZ(14), fill=C(150,165,190))
        _pd = pdate if pdate else "从未"
        _pdf = F(15) if pdate else FZ(15)
        d.text((_RX+44, _ry-1), _pd, font=_pdf, fill=C(215,225,240))
        _ry += 21
        if pspeed>0:
            _st = f"{pspeed:.0f}MB/s"; _sf = F(15); _sc = C(120,205,255)
        else:
            _st = "空闲"; _sf = FZ(15); _sc = C(158,172,196)
        d.text((_RX+2, _ry), _st, font=_sf, fill=_sc)
        _ec = GRN if perr==0 else RED
        _et = f"错误 {perr}"
        _ef = FZ(15)
        d.text((_RX+_COLW-6-TW(_et,_ef), _ry), _et, font=_ef, fill=_ec)
        _ry += 21
        d.text((_RX+2, _ry), "下次", font=FZ(14), fill=C(150,165,190))
        d.text((_RX+44, _ry-1), pnext, font=FZ(15), fill=C(195,208,228))
        _ry += 20

    y = max(_dy, _ry) + 4

    y = sep(y, 6)

    def clip(txt, fnt, maxw):
        """按实际像素宽度截断，超出加 .."""
        s = txt
        if TW(s, fnt) <= maxw:
            return s
        while len(s) > 1 and TW(s + "..", fnt) > maxw:
            s = s[:-1]
        return s + ".."

    # ===== 影视库 =====
    d.text((16,y), "影视库", font=FZ(19), fill=DIM)
    _emby_hdr = y
    y += 26

    # 下方 VM + 容器占用固定高度，反推影视库卡片可用高度
    _vm_h  = 6 + 24 + 40 + 6
    _dk_n  = len(cn)
    _dk_pc = (_dk_n + 1) // 2
    _dk_h  = 26 + _dk_pc*25 - 4
    _below = _vm_h + _dk_h + 8
    _card_h = max(80, VH - 8 - y - _below)

    # 布局基准：整屏 600 宽，左右边距 16 / 8
    _VW  = VW
    _LM  = _LAYOUT["margin_left"]      # 左边距（layout.json: margin_left）
    _RM  = _LAYOUT["margin_right"]     # 右边距（layout.json: margin_right）
    _GAP = _LAYOUT["gap"]              # 播放卡片/海报间隙（layout.json: gap）
    _FULL = _VW - _LM - _RM       # 576 可用总宽
    _UNIT = (_FULL - 2 * _GAP) // 3   # 1/3 单元宽（扣掉两个间隙）

    _pw_y1 = y
    _pw_y2 = y + _card_h

    # 海报专用垂直范围：上沿齐"影视库"标题上方，下沿停在「虚拟机」标题栏下沿
    # 虚拟机标题占 y+1 ~ y+21，随后 y += 24 开始画卡片；
    # 故底边取 +24（原先 +30 会伸进虚拟机卡片 6px，看着被压住）
    _post_y1 = _emby_hdr
    _post_y2 = y + _card_h + 6 + _LAYOUT["poster_bottom_pad"]

    # ---- 播放路数决定布局 ----
    # 0 路：海报占满；1 路：播放 1/3、海报 2/3；2 路：播放 2/3、海报 1/3；
    # 3 路：播放占满、无海报；>3 路：同屏 3 个，整体滚动
    _n_all = 0 if eb is None else len(eb)
    # 用户规则：1 路=占 1/3（右侧大海报）；2 路=各占整宽 1/2；>=3 路不显示。
    _SHOW = 0 if _n_all >= 3 else _n_all
    if _SHOW == 2:
        _UNIT = (_FULL - _GAP) // 2       # 两人平分整个影视库栏
    else:
        _UNIT = (_FULL - 2 * _GAP) // 3   # 单路保持 1/3
    _play_w = 0 if _SHOW == 0 else _UNIT * _SHOW + _GAP * (_SHOW - 1)
    if _SHOW == 2:
        _play_w = _FULL                   # 两路正好铺满整个宽度
    _post_x1 = _LM + (_play_w + _GAP if _play_w > 0 else 0)
    _post_x2 = _VW - _RM
    # 无播放时海报左边界紧跟虚拟机计数「x/y」右侧 12px，右边界不变
    if _SHOW == 0:
        _vm_run_pre = sum(1 for _, _r in vm if _r)
        _vm_cnt_txt = f"{_vm_run_pre}/{len(vm)}"
        _pre_x = 82 + TW(_vm_cnt_txt, F(19)) + 12
        _post_x1 = _pre_x

    FW = _play_w                                  # 播放区总宽

    # ---------- 左：播放区 ----------
    if _SHOW == 0:
        d.text((_LM + 2, y + 2), "空闲", font=FZ(20), fill=C(158, 172, 196))
        d.text((_LM + 2, y + 28), "当前无播放", font=FZ(15), fill=C(140, 155, 178))
    else:
        _now = time.time()

        def _scroll(txt, fnt, maxw, speed=28):
            w = TW(txt, fnt)
            if w <= maxw:
                return txt, 0
            span = w - maxw
            cycle = (span + 44) / speed
            ph = (_now % cycle) * speed - 22
            return txt, int(max(0, min(span, ph)))

        _pg = _GAP
        _cw_play = _UNIT

        # 最多显示 2 路，不做横向滚动（>=3 路时 _SHOW 已置 0）
        _slide = 0

        for _i, sess in enumerate(eb[:_SHOW]):
            eb_title, eb_sub, eb_pct, eb_paused, eb_client, eb_user = sess[:6]
            eb_img = sess[6] if len(sess) > 6 else None
            px = _LM + _i * (_cw_play + _pg) - _slide
            # 卡片裁切到播放区右边界内
            _cx_lo = _LM
            _cx_hi = _LM + FW
            if px + _cw_play <= _cx_lo or px >= _cx_hi:
                continue
            bar_x1 = px + 8
            bar_x2 = px + _cw_play - 8
            bar_w  = bar_x2 - bar_x1
            bar_y  = y + _card_h - 32
            # 双路播放时进度条只占卡片 40%，右侧腾给封面；
            # 百分比文字紧跟进度条尽头（左对齐），不再贴卡片右缘。
            _dbl = (_SHOW == 2)
            if _dbl:
                bar_w2 = int(bar_w * 0.40)
                bar_x2 = bar_x1 + bar_w2
                bar_w  = bar_w2

            # ---- 多路(>=2)：左文字 / 右封面，泾渭分明不重叠 ----
            # 卡内可用区：x 从 px+8 到 px+_cw_play-8（与进度条同宽）
            # 纵向可用区：y+2 ~ bar_y-4（顶到卡顶，底抵进度条上方）
            _PADL, _PADR = 8, 8
            _inner_w = _cw_play - _PADL - _PADR
            _inner_x0 = px + _PADL
            # 双路封面顶部上提到"影视库"标题齐平（_emby_hdr），单路保持卡内原样
            _cov_top = _emby_hdr if _dbl else (y + 2)
            # 双路封面底边分两种：
            #  · 窄封面（左缘越过进度条右端）不会碰到进度条 → 下探到卡片底部
            #  · 宽封面（横向跨到进度条区域）→ 必须停在进度条上方
            # 先按"下探到底"给高度，算完宽度后再按需回退。
            _cov_bot = (y + _card_h + 27) if _dbl else (bar_y - 4)
            _cov_h = _cov_bot - _cov_top
            _pct_right = bar_x2          # 百分比右对齐条右端

            _cov = None
            _txt_w = _inner_w           # 单路：文字占满整卡（保持原样）
            if _SHOW >= 2 and eb_img and _cov_h >= 24:
                # 封面按图片真实宽高比、以纵向铺满 _cov_h 为基准算宽；
                # 文字吃剩下的全部宽度（保证至少 _TXT_MINW），优先让封面大。
                _gap_tc = 8
                _TXT_MINW = 66                 # 文字列最小宽（约 4 个中文字，超长自动滚动）
                _cov_limit = _inner_w - _TXT_MINW - _gap_tc
                _ar = 2 / 3
                _raw_p = play_image(eb_img.get("kind"), eb_img.get("id"))
                if _raw_p:
                    try:
                        import io as _io3
                        _tm = Image.open(_io3.BytesIO(_raw_p))
                        if _tm.height > 0:
                            _ar = _tm.width / _tm.height
                    except Exception:
                        pass
                # 竖版(ar<1)：在原比例宽和可用上限之间取折中，
                # 既放大视觉面积，又不至于把图裁得面目全非。
                _w_ratio = int(_cov_h * _ar)
                _w_cap = max(16, _cov_limit)
                if _ar < 0.85:
                    # 目标 = 比例宽 + (上限-比例宽) 的 70%，且不超过上限
                    _grown = _w_ratio + int((_w_cap - _w_ratio) * 0.70)
                    _cov_w = max(_w_ratio, min(_grown, _w_cap))
                else:
                    _cov_w = _w_ratio
                _cov_w = max(16, min(_cov_w, _w_cap))
                # 封面要下探到进度条以下时，宽度不能越过进度条右端，
                # 否则会横跨在进度条/百分比上。优先缩宽保住纵向满高。
                if _dbl and _cov_bot > bar_y - 4:
                    _x0_chk = px + _cw_play - _PADR - _cov_w
                    _need_x0 = bar_x2 + 4              # 左缘必须在这条线右边
                    if _x0_chk < _need_x0:
                        _w_max2 = _cw_play - _PADR - (_need_x0 - px)
                        _w_max2 = max(16, _w_max2)
                        if _w_max2 >= int(_cov_h * 0.55):
                            _cov_w = min(_cov_w, _w_max2)
                        else:
                            # 缩到太窄不好看，则回退到进度条上方
                            _bot2 = bar_y - 4
                            _h2 = _bot2 - _cov_top
                            if _h2 > 16:
                                _w3 = max(16, min(int(_h2 * _ar),
                                                  max(16, _inner_w - _TXT_MINW - _gap_tc)))
                                _cov_bot, _cov_h, _cov_w = _bot2, _h2, _w3
                _cov = dict(top=_cov_top, bottom=_cov_bot,
                            h=_cov_h, w=_cov_w,
                            x0=px + _cw_play - _PADR - _cov_w)
                # 文字宽度固定按"卡内宽 - 封面预留"计算，不跟封面实际宽度联动。
                # 这样封面图异步加载完成前后，文字区宽度恒定，不会跳。
                _txt_w = max(_TXT_MINW, _inner_w - _TXT_MINW - _gap_tc)
            _txt_x0_off = _PADL
            _avw = max(24, _txt_w)

            # 第1行：剧名 20 号，超长滚动
            tf = FZ(20)
            _tt, _toff = _scroll(eb_title, tf, _avw)
            d.text((px + _txt_x0_off - _toff, y + 2), _tt, font=tf, fill=C(235, 243, 252))

            # 第2行：季集 16 号
            _ep_no, _ep_name = eb_sub, ""
            for _sep in (" - ", " -", "- ", "—"):
                if _sep in eb_sub:
                    _a, _b = eb_sub.split(_sep, 1)
                    _ep_no, _ep_name = _a.strip(), _b.strip()
                    break
            sf = FZ(16)
            _st, _soff = _scroll(_ep_no, sf, _avw)
            d.text((px + _txt_x0_off - _soff, y + 30), _st, font=sf, fill=C(175, 188, 208))

            # 第3行：集名。必须严格排在季集行(y+30,字高16)之下、进度条(bar_y)之上；
            # 空间不足时宁可整行不画，也不要压到第2行上造成堆叠。
            if _ep_name:
                ef = FZ(15)
                _ep_line_top = y + 30 + 18          # 季集行(高16) + 2px 行距
                _ep_avail = bar_y - _ep_line_top     # 集名可用净高
                if _ep_avail >= 15:
                    _et, _eoff = _scroll(_ep_name, ef, _avw)
                    d.text((px + _txt_x0_off - _eoff, _ep_line_top), _et,
                           font=ef, fill=C(158, 172, 196))

            # ---- 封面：贴右缘，纵向顶到 y+2、底抵进度条上方，横向铺满剩余 ----
            if _cov is not None and _cov.get("w"):
                try:
                    import io as _io4
                    _rp = play_image(eb_img.get("kind"), eb_img.get("id"))
                    if _rp:
                        _ci = Image.open(_io4.BytesIO(_rp)).convert("RGB")
                        _cwv = _cov["w"]; _chv = _cov["h"]
                        _sc = max(_cwv / _ci.width, _chv / _ci.height)
                        _nw = max(1, int(_ci.width * _sc))
                        _nh = max(1, int(_ci.height * _sc))
                        _ci = _ci.resize((_nw, _nh), Image.LANCZOS)
                        _ox = max(0, (_nw - _cwv) // 2)
                        _oy = max(0, (_nh - _chv) // 2)
                        _ci = _ci.crop((_ox, _oy, _ox + _cwv, _oy + _chv))
                        img.paste(_ci, (_cov["x0"], _cov["top"]))
                        d.rectangle([_cov["x0"], _cov["top"],
                                     _cov["x0"] + _cwv - 1, _cov["top"] + _chv - 1],
                                    outline=C(44, 56, 80), width=1)
                except Exception:
                    pass

            # 进度条：横向铺满卡片
            d.rectangle([bar_x1, bar_y, bar_x2, bar_y + 5], fill=C(40, 52, 72))
            _fw_px = int(bar_w * max(0, min(100, eb_pct)) / 100.0)
            if _fw_px > 0:
                d.rectangle([bar_x1, bar_y, bar_x1 + _fw_px, bar_y + 5],
                            fill=ORG if eb_paused else GRN)

            st_txt = "暂停" if eb_paused else f"{eb_pct}%"
            st_fill = ORG if eb_paused else GRN
            stf = FZ(15) if eb_paused else F(15)
            if _dbl:
                # 双路：百分比画在进度条「上方」，右对齐进度条右端；
                # 字号 14，高度不超过上面的集名行(15)与季集行(16)。
                spf = FZ(14) if eb_paused else F(14)
                d.text((bar_x2 - TW(st_txt, spf), bar_y - 17),
                       st_txt, font=spf, fill=st_fill)
            else:
                d.text((bar_x2 - TW(st_txt, stf), bar_y + 8), st_txt, font=stf, fill=st_fill)

            # 第4行：用户名（青色）+ 客户端
            # 用户名与客户端一律固定左对齐、绝不滚动（滚动会造成视觉抖动）。
            # 宽度不够时按像素截断加".."，位置永远锚定在 px + _txt_x0_off。
            uf = FZ(16)
            who_user = eb_user if eb_user else "?"
            # 客户端与用户名同字体同字号（原来用 DejaVu 15 号，比用户名小一号）
            cf = FZ(16)
            _cneed = TW(eb_client, cf) if eb_client else 0
            # 固定可用宽度：不用 _avw（它随封面宽度变化），直接用卡片内宽
            _row_avw = _cw_play - _PADL - _PADR
            _user_maxw = _row_avw if not eb_client else max(24, _row_avw - _cneed - 8)
            _uname = who_user
            if TW(_uname, uf) > _user_maxw:
                while _uname and TW(_uname + "..", uf) > _user_maxw:
                    _uname = _uname[:-1]
                _uname = (_uname + "..") if _uname else who_user[:1]
            d.text((px + _txt_x0_off, y + _card_h - 20), _uname, font=uf,
                   fill=C(0, 200, 255))
            if eb_client:
                _uw_fix = TW(_uname, uf)
                _cx_cli = px + _txt_x0_off + _uw_fix + 8
                # 客户端也不能超出卡片右内边
                if _cx_cli + _cneed <= px + _cw_play - _PADR:
                    d.text((_cx_cli, y + _card_h - 19), eb_client,
                           font=cf, fill=C(150, 165, 190))

    # ---------- 右：当前播放内容的海报（仅 1 路播放时）----------
    # 电影用封面，剧集用当前集的图；横版铺满 _post_x1.._post_x2 区域。
    if _SHOW == 1 and eb and _post_x2 > _post_x1 + 40:
        try:
            _s7 = eb[0]
            _img = _s7[6] if len(_s7) > 6 else None
            _raw = None
            if _img:
                _raw = play_image(_img.get("kind"), _img.get("id"))
            if _raw:
                import io as _io
                _im = Image.open(_io.BytesIO(_raw)).convert("RGB")
                _bw = _post_x2 - _post_x1
                _bh = _post_y2 - _post_y1
                if _bw > 20 and _bh > 20:
                    # cover 缩放：铺满区域，超出部分居中裁切
                    _sc = max(_bw / _im.width, _bh / _im.height)
                    _nw = max(1, int(_im.width * _sc))
                    _nh = max(1, int(_im.height * _sc))
                    _im2 = _im.resize((_nw, _nh), Image.LANCZOS)
                    _ox = (_nw - _bw) // 2
                    _oy = (_nh - _bh) // 2
                    _im2 = _im2.crop((_ox, _oy, _ox + _bw, _oy + _bh))
                    img.paste(_im2, (_post_x1, _post_y1))
                    # 细边框，和整体风格一致
                    d.rectangle([_post_x1, _post_y1,
                                 _post_x1 + _bw - 1, _post_y1 + _bh - 1],
                                outline=C(44, 56, 80), width=1)
        except Exception:
            pass
    # ---------- 海报墙已取消（按用户要求移除贴图）----------

    y += _card_h + 6

    # ===== VM =====
    vm_run=sum(1 for _,r in vm if r)
    vh_f = FZ(17)
    d.text((16,y+1), "虚拟机", font=vh_f, fill=DIM)
    vc = GRN if vm_run==len(vm) else ORG
    CNT_X = 82                                 # 计数列，给"虚拟机"留足空间
    d.text((CNT_X,y-2), f"{vm_run}/{len(vm)}", font=F(19), fill=vc)
    y += 24
    for i,(n,r) in enumerate(vm):
        xx=16+i*284
        col = GRN if r else RED
        d.rectangle([xx,y,xx+276,y+30], fill=C(24,32,50))
        d.ellipse([xx+10,y+9,xx+21,y+20], fill=col)
        d.text((xx+30,y+3), n, font=F(18), fill=C(215,225,240))
        s="运行中" if r else "已停止"
        d.text((xx+266-TW(s,FZ(16)), y+5), s, font=FZ(16), fill=col)
    y += 40

    y = sep(y, 6)

    # ===== Docker（下移，两排）=====
    run_n=sum(1 for _,r,_ in cn if r)
    dh_f = FZ(17)
    d.text((16,y+1), "容器", font=dh_f, fill=DIM)
    badge=f"{run_n}/{len(cn)}"
    fb_=F(19)
    bx = 82                                    # 和虚拟机同列
    d.text((bx,y-2), badge, font=fb_, fill=GRN if run_n==len(cn) else ORG)
    d.text((bx+TW(badge,fb_)+8, y+1),
           "全部运行" if run_n==len(cn) else f"{len(cn)-run_n} 个已停",
           font=FZ(14), fill=GRN if run_n==len(cn) else RED)
    y += 26

    N=len(cn)
    percol=(N+1)//2
    colw=(VW-40)//2            # 左右各留 12px 安全边距，避让 3px 边框
    row_h=25
    f_dur=F(15); dur_sz=15
    for idx,(n,r,dur) in enumerate(cn):
        cidx = 0 if idx<percol else 1
        ridx = idx if cidx==0 else idx-percol
        xx = 18 + cidx*(colw+6)
        yy = y + ridx*row_h
        col = GRN if r else RED
        if ridx%2==0:
            d.rectangle([xx-2,yy-1,xx+colw-2,yy+row_h-3], fill=C(26,35,54))
        d.ellipse([xx+2,yy+7,xx+11,yy+16], fill=col)
        # 名字可用宽度 = 列宽 - 左侧圆点/内缩 - 右侧时长 - 间隙
        # 现算时长（stamp 是启动时间戳）；停机的容器不显示时长
        if r and dur:
            ds = fmt_sec(time.time()-dur)
        elif r:
            ds = "运行"
        else:
            ds = "--"
        fw=d.textbbox((0,0),ds,font=FZ(dur_sz))[2]
        name_x = xx+16
        name_maxw = (xx+colw-6-fw-10) - name_x
        f_name=F(16)
        nm = n
        if d.textbbox((0,0),nm,font=f_name)[2] > name_maxw:
            # 逐字回退，直到能塞下（末尾留 '..'）
            for cut in range(len(n)-1, 0, -1):
                cand = n[:cut]+".."
                if d.textbbox((0,0),cand,font=f_name)[2] <= name_maxw:
                    nm = cand
                    break
            else:
                nm = n[:6]+".."
        d.text((name_x,yy+1), nm, font=f_name, fill=C(215,225,240))
        f_dur_zh = FZ(dur_sz)
        d.text((xx+colw-6-TW(ds,f_dur_zh), yy+3), ds, font=f_dur_zh, fill=GRN if r else RED)
    y += percol*row_h - 4

    # ===== 输出 =====
    # fb0 是 2560x1440 / stride 10240B，屏幕只显示左上角 1024x600，
    # 但每行必须按 10240 字节对齐写入，否则会花屏（曾踩过坑）。
    # 这里构造整幅画布，贴到 (0,0)，其余保持黑色。
    rot = img.rotate(-90, expand=True)          # 600x1024 -> 1024x600
    rot = rot.rotate(180)                       # 再整体转 180 度（屏幕倒装）
    # fb0 实际就是 1024x600 / stride 4096，直接输出即可，无需大画布
    # fb0 是 DRM XR24 little-endian：内存字节序为 B,G,R,X，
    # 而 PIL 的 RGBA 输出 R,G,B,A —— 必须红蓝互换，否则整屏偏色。
    # 只有真正需要连续动画时才用高帧率：
    #   1) 剧名/季集/集名文字过长需要滚动
    #   2) 播放路数超过 3，卡片整体滚动
    # 海报是静态的（24 小时才换），不参与判断。
    _anim = False
    try:
        if eb and len(eb) > 3:
            _anim = True
        elif eb:
            for _sess in eb:
                if TW(_sess[0], FZ(20)) > 170 or TW(_sess[1], FZ(16)) > 170:
                    _anim = True
                    break
    except Exception:
        _anim = False
    return rot.convert("RGB").tobytes("raw", "BGRX"), y, _anim

def main():
    try: os.makedirs("/app/logs", exist_ok=True)
    except Exception: pass
    log=open("/app/logs/panel.log","a",buffering=1)
    log.write(f"=== start {time.strftime('%F %T')} ===\n")
    # ---- 启动时夺取屏幕控制权 ----
    def _takeover():
        # 1) fbcon 会持续把控制台文字画到 framebuffer，必须解绑
        try:
            with open("/sys/class/graphics/fbcon/cursor_blink","w") as f:
                f.write("0")
            log.write("fbcon cursor off\n")
        except Exception as e:
            log.write(f"cursor off failed: {e}\n")
        # 2) 解绑 vtconsole 的 framebuffer 后端（vtcon1）
        try:
            p="/sys/class/vtconsole/vtcon1/bind"
            if open(p).read().strip()=="1":
                with open(p,"w") as f:
                    f.write("0")
                log.write("fbcon unbound\n")
        except Exception as e:
            log.write(f"unbind failed: {e}\n")
        # 3) 取消 framebuffer 断电
        try:
            with open("/sys/class/graphics/fb0/blank","w") as f:
                f.write("0")
            log.write("blank=0\n")
        except Exception as e:
            log.write(f"blank failed: {e}\n")

    _takeover()

    # 清掉屏幕上残留的启动日志（fbcon 解绑后旧字会冻结在屏幕上）
    try:
        nbytes = W*H*4
        with open(FB,"r+b",buffering=0) as f:
            CH = 262144                     # 256KB 小块，避免一次性大分配
            z = b"\x00" * CH
            left = nbytes
            while left > 0:
                k = CH if left >= CH else left
                f.write(z[:k] if k < CH else z)
                left -= k
        log.write(f"screen cleared ({nbytes} bytes)\n")
    except Exception as e:
        log.write(f"clear failed: {e}\n")
    _img_start_watcher()          # 启动 docker events 监听线程
    log.write("img watcher: started\n")
    _poster_start(log)            # 海报墙后台抓取线程
    log.write("poster worker: started\n")
    _playimg_start(log)
    log.write("playimg worker: started\n")
    running=True
    def stop(sig,frame):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    import atexit
    def _cleanup():
        p=_img_proc.get("p")
        if p and p.poll() is None:
            try: p.terminate()
            except Exception: pass
    atexit.register(_cleanup)

    # 内核 consoleblank 默认 900 秒会把 fb 断电，导致屏幕停在旧画面。
    # 每秒检测并强制恢复显示。
    def _keep_awake():
        try:
            with open("/sys/class/graphics/fb0/blank") as f:
                v = f.read().strip()
            if v != "0":
                with open("/sys/class/graphics/fb0/blank","w") as f:
                    f.write("0")
                log.write(f"blank {v} -> 0 @ {time.strftime('%T')}\n")
        except Exception:
            pass

    _blank_tick = 0
    _sig_prev = None
    _anim_prev = False
    while running:
        try:
            _t0 = time.time()
            _sync_proc()
            # 静态画面里唯一每秒变的是右上角时钟；同一秒内重复渲染纯属浪费。
            # 用"时钟秒 + 动画标志"做指纹，没变就整帧跳过（不 render、不写 fb0）。
            _sig = (time.strftime("%H:%M:%S"), _anim_prev)
            _need = (_sig != _sig_prev)
            _anim = _anim_prev
            if _need:
                data,yend,_anim=render()
                with open(FB,"r+b",buffering=0) as f:
                    f.write(data)
                _sig_prev = (time.strftime("%H:%M:%S"), _anim)
                _anim_prev = _anim
            # 每 150 帧（约 5 秒）检查一次 blank，不必每帧读 sysfs
            _blank_tick += 1
            if _blank_tick % 50 == 0:
                _keep_awake()
            # 10fps 节拍：补偿渲染耗时，稳定 100ms/帧
            _el = time.time() - _t0
            # 静态时由于有指纹跳过，实际渲染约 1 次/秒；轮询用 1.2fps 足够
            _fps = _LAYOUT["fps_anim"] if _anim else _LAYOUT["fps_static"]
            _sl = (1.0/_fps) - _el
            if _sl > 0.002:
                time.sleep(_sl)
        except Exception as e:
            log.write(f"ERR {time.strftime('%T')}: {e}\n")
            time.sleep(3)
    log.write(f"=== stopped {time.strftime('%F %T')} ===\n")

if __name__=="__main__":
    main()
