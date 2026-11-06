#!/bin/bash
# 宿主机数据采集：把 Unraid 各工具的输出整理成 JSON，供容器 unRAID-FP 读取
# 容器内无法调用 mdcmd/smartctl/virsh/docker，故由宿主机代劳
OUT=/mnt/cache/appdata/unRAID-FP/data/state.json
TMP=$OUT.tmp

md=$(/usr/local/sbin/mdcmd status 2>/dev/null)
vms=$(/usr/bin/virsh list --all 2>/dev/null)
cts=$(/usr/bin/docker ps -a --format '{{.Names}}|{{.State}}|{{.State.StartedAt}}' 2>/dev/null)
ims=$(/usr/bin/docker images -q 2>/dev/null | wc -l)
fans=$(/usr/bin/sensors 2>/dev/null)

python3 - "$OUT" "$TMP" <<'PY'
import sys, json, os, re, subprocess, time
out, tmp = sys.argv[1], sys.argv[2]

def sh(c, t=10):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t).stdout
    except Exception:
        return ""

# --- mdcmd status -> 阵列/校验 ---
md = sh("/usr/local/sbin/mdcmd status 2>/dev/null")
mdct = {}
for line in md.splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        mdct[k.strip()] = v.strip()

# --- 磁盘信息 ---
disks = []
try:
    cur = None
    ini = {}
    for line in open("/var/local/emhttp/disks.ini", encoding="utf-8", errors="ignore"):
        line = line.strip()
        if line.startswith('["') and line.endswith('"]'):
            cur = line[2:-2]; ini[cur] = {}
        elif cur and "=" in line:
            k, v = line.split("=", 1)
            ini[cur][k.strip()] = v.strip().strip('"')
    CN = {"parity":"校验盘","parity2":"校验2","disk1":"硬盘1","disk2":"硬盘2",
          "disk3":"硬盘3","disk4":"硬盘4","cache":"缓存盘","flash":"U盘"}
    for key in ("cache","parity","disk1","disk2","disk3","disk4"):
        f = ini.get(key)
        if not f: continue
        t = f.get("temp","")
        if not t.isdigit():
            # ini 无温度（休眠盘或未刷新），用 smartctl -n standby 补，休眠盘直接跳过不唤醒
            dev = f.get("device","")
            if dev:
                try:
                    pr = subprocess.run(["/usr/sbin/smartctl","-n","standby","-A","/dev/"+dev],
                                        capture_output=True, text=True, timeout=8)
                    so = pr.stdout or ""
                    if pr.returncode != 2 and "STANDBY" not in so.upper():
                        mm = re.search(r"^\s*194\s+Temperature_Celsius\s+\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)", so, re.M|re.I)
                        if not mm:
                            mm = re.search(r"^\s*190\s+Airflow_Temperature\S*\s+\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)", so, re.M|re.I)
                        if mm:
                            t = mm.group(1)
                except Exception:
                    pass
        disks.append({
            "name": CN.get(key,key),
            "device": f.get("device",""),
            "temp": int(t) if t.isdigit() else None,
            "status": f.get("status",""),
            "fsUsed": int(f["fsUsed"])*1024 if f.get("fsUsed","").isdigit() else 0,
            "fsFree": int(f["fsFree"])*1024 if f.get("fsFree","").isdigit() else 0,
            "size": int(f["size"])*512 if f.get("size","").isdigit() else 0,
        })
except Exception as e:
    disks.append({"err": str(e)})

# --- 容器列表（用 list 形式传参，避免 {{}} 被 shell 吃掉）---
containers = []
try:
    # 名称+状态（ps 不提供 StartedAt，需 inspect）
    o = subprocess.run(["/usr/bin/docker","ps","-a","--format","{{.Names}}|{{.State}}"],
                       capture_output=True, text=True, timeout=15).stdout
    names, states = [], {}
    for line in o.strip().splitlines():
        if "|" not in line: continue
        nm, st = line.split("|", 1)
        names.append(nm); states[nm] = (st == "running")
    # 批量取精确启动时间
    started = {}
    if names:
        o2 = subprocess.run(["/usr/bin/docker","inspect","--format",
                             "{{.Name}}|{{.State.StartedAt}}"] + names,
                            capture_output=True, text=True, timeout=20).stdout
        for line in o2.strip().splitlines():
            if "|" not in line: continue
            nm, ts = line.split("|", 1)
            started[nm.lstrip("/")] = ts.strip()
    for n in names:
        containers.append({"name": n, "running": states.get(n, False),
                           "started": started.get(n, "")})
except Exception:
    pass

# --- 镜像数 ---
img_count = 0
img_gb = 0.0
img_reclaim_gb = 0.0
try:
    ids = [x for x in sh("/usr/bin/docker images -q 2>/dev/null").split() if x]
    img_count = len(ids)
    def _to_gb(t):
        t = (t or "").strip()
        try:
            if t.endswith("GB"): return float(t[:-2])
            if t.endswith("MB"): return float(t[:-2]) / 1024
            if t.endswith("kB"): return float(t[:-2]) / 1024 / 1024
            if t.endswith("B"):  return float(t[:-1]) / 1024 / 1024 / 1024
        except Exception:
            pass
        return 0.0
    # docker system df 极贵（实测 1157ms），但它统计的镜像/卷占用变化极慢，
    # 没必要每 8 秒重算。这里缓存 600 秒，避免把 dockerd 持续压在 40% CPU。
    _DF_CACHE = "/tmp/_unraid_fp_df_cache"
    dfout = ""
    try:
        _age = time.time() - os.path.getmtime(_DF_CACHE)
        if _age < 600:
            dfout = open(_DF_CACHE).read()
    except Exception:
        pass
    if not dfout.strip():
        # 断路器：docker system df 卡死时（06:56 dockerd Oops 后永久 hang），
        # 失败后 1 小时内不再重试，避免每次 timeout 残留一个卡在 futex 的孤儿进程。
        _skip = False
        _DF_FAIL = "/tmp/_unraid_fp_df_fail"
        try:
            _skip = (time.time() - os.path.getmtime(_DF_FAIL)) < 3600
        except Exception:
            pass
        if not _skip:
            # 用 timeout 命令确保连 docker CLI 一起杀掉，不留孤儿
            dfout = sh("/usr/bin/timeout -k 2 20 /usr/bin/docker system df --format '{{.Type}}|{{.Size}}|{{.Reclaimable}}' 2>/dev/null", 25)
            try:
                if dfout.strip():
                    open(_DF_CACHE, "w").write(dfout)
                else:
                    open(_DF_FAIL, "w").write("1")
            except Exception:
                pass
    for line in dfout.strip().splitlines():
        p = line.split("|")
        if len(p) < 3: continue
        if p[0].strip().lower() == "images":
            img_gb = _to_gb(p[1])
            img_reclaim_gb = _to_gb(p[2].split("(")[0])
except Exception:
    pass

# --- 虚拟机 ---
vms = []
for l in sh("/usr/bin/virsh list --all 2>/dev/null").splitlines()[2:]:
    p = l.split()
    if len(p) >= 3:
        vms.append({"name": p[1], "running": p[2]=="running"})

# --- 风扇 ---
fan = 0
m = re.search(r"fan2:\s+(\d+)", sh("/usr/bin/sensors 2>/dev/null"))
if m: fan = int(m.group(1))

# --- 存储容量（df） ---
storage = {}
o = sh("/bin/df -BG --output=target,size,used,pcent /mnt/cache /mnt/user 2>/dev/null")
for line in o.strip().splitlines()[1:]:
    p = line.split()
    if len(p) < 4: continue
    key = "cache" if "cache" in p[0] else "array"
    try:
        storage[key] = {"pct": int(p[3].rstrip("%")),
                        "used": float(p[2].rstrip("G")),
                        "size": float(p[1].rstrip("G"))}
    except Exception:
        pass

# --- 校验历史 ---
parity_log = ""
try:
    parity_log = open("/boot/config/parity-checks.log").read()
except Exception:
    pass

# --- 系统信息（CPU/内存/负载/运行时间/网络）---
def _rd(p):
    try: return open(p).read()
    except Exception: return ""

_stat_txt = _rd("/proc/stat")

# --- CPU 瞬时占用：对比上次采集的 /proc/stat ---
# 容器内渲染帧率远快于采集周期，若让容器自己算差值会因数据未变而恒为 0，
# 所以在这里（采集端）算好百分比，容器直接读用。
_CPU_PREV_FILE = "/tmp/_unraid_fp_cpu_prev"
_cpu_pct = 0.0
try:
    _v = [int(x) for x in _stat_txt.split("\n")[0].split()[1:]]
    _tot, _idle = sum(_v), _v[3] + _v[4]
    _prev = None
    try:
        with open(_CPU_PREV_FILE) as _f:
            _a, _b = _f.read().split()
            _prev = (int(_a), int(_b))
    except Exception:
        _prev = None
    if _prev:
        _dt, _di = _tot - _prev[0], _idle - _prev[1]
        if _dt > 0:
            _cpu_pct = max(0.0, min(100.0, (1 - _di / _dt) * 100))
    with open(_CPU_PREV_FILE, "w") as _f:
        _f.write(f"{_tot} {_idle}")
except Exception:
    _cpu_pct = 0.0

# --- CPU 功耗（Intel RAPL 能量计数器差分，单位 W）---
# energy_uj 是累计微焦，且会在 max_energy_range_uj 处回绕，这里做差分+回绕修正。
_PWR_ENERGY = "/sys/class/powercap/intel-rapl:0/energy_uj"
_PWR_RANGE = "/sys/class/powercap/intel-rapl:0/max_energy_range_uj"
_PWR_PREV_FILE = "/tmp/_unraid_fp_pwr_prev"
_pwr_w = 0.0
try:
    _e = int(_rd(_PWR_ENERGY).strip())
    _rg = 262143328850
    try:
        _rg = int(_rd(_PWR_RANGE).strip())
    except Exception:
        pass
    _pt = time.time()
    _pe = None
    try:
        with open(_PWR_PREV_FILE) as _f:
            _a, _b = _f.read().split()
            _pe, _pt = int(_a), float(_b)
    except Exception:
        _pe = None
    if _pe is not None:
        _de = _e - _pe
        if _de < 0:
            _de += _rg              # 计数器回绕
        _dtw = time.time() - _pt
        if _dtw > 0.5 and _de >= 0:
            _pwr_w = max(0.0, min(600.0, (_de / 1e6) / _dtw))
    with open(_PWR_PREV_FILE, "w") as _f:
        _f.write(f"{_e} {time.time()}")
except Exception:
    _pwr_w = 0.0

sysinfo = {
    "loadavg": _rd("/proc/loadavg").strip(),
    "meminfo": _rd("/proc/meminfo"),
    "uptime":  _rd("/proc/uptime").strip(),
    "netdev":  _rd("/proc/net/dev"),
    "stat":    _stat_txt,
    "diskstats": _rd("/proc/diskstats"),
    "cpu_pct": f"{_cpu_pct:.1f}",
    "cpu_w":   f"{_pwr_w:.1f}",
}

# --- 下次校验计划（Unraid 的 parity-check.cron）---
_pcron = ""
try:
    _pcron = open("/boot/config/plugins/dynamix/parity-check.cron").read()
except Exception:
    pass

# --- 磁盘/网络实时速率（宿主机端计算，8秒窗口）---
_IO_STATE = "/root/.agent-io-prev"
io_rate = {}
net_rate_h = {"rx": 0.0, "tx": 0.0}
try:
    import time as _t
    cur_ts = _t.time()
    cur = {}
    for l in _rd("/proc/diskstats").splitlines():
        f = l.split()
        if len(f) < 14: continue
        dev = f[2]
        if not (dev.startswith("sd") or dev.startswith("nvme")): continue
        # 排除分区：sdX 后面不能再有数字；nvme0n1p1 这种才是分区
        if len(dev) != 3 and not dev.startswith("nvme"): continue
        if dev.startswith("nvme") and "p" in dev: continue
        cur[dev] = (int(f[5]), int(f[9]))
    rx = tx = 0
    for l in _rd("/proc/net/dev").splitlines():
        if ":" not in l: continue
        nm, rest = l.split(":", 1)
        if nm.strip() != "br0": continue
        p = rest.split()
        rx, tx = int(p[0]), int(p[8])
        break
    prev = None
    try:
        with open(_IO_STATE) as f:
            prev = json.load(f)
    except Exception:
        prev = None
    if prev and cur_ts - prev.get("ts", 0) > 0.5:
        dt = cur_ts - prev["ts"]
        for dev, (rs, ws) in cur.items():
            if dev in prev.get("d", {}):
                prs, pws = prev["d"][dev]
                io_rate[dev] = [
                    round(max(0, rs - prs) * 512 / dt / 1048576, 3),
                    round(max(0, ws - pws) * 512 / dt / 1048576, 3),
                ]
        if prev.get("rx") is not None:
            net_rate_h = {
                "rx": round(max(0, rx - prev["rx"]) / dt / 1048576, 3),
                "tx": round(max(0, tx - prev["tx"]) / dt / 1048576, 3),
            }
    with open(_IO_STATE, "w") as f:
        json.dump({"ts": cur_ts, "d": cur, "rx": rx, "tx": tx}, f)
except Exception:
    pass

data = {
    "ts": __import__("time").time(),
    "sys": sysinfo,
    "md": {k: mdct.get(k) for k in
           ("mdState","mdResync","mdResyncSize","mdResyncAction","mdResyncPos",
            "mdResyncDt","mdResyncDb","mdResyncCorr","mdNumDisks","mdNumDisabled")},
    "disks": disks,
    "containers": containers,
    "img_count": img_count,
    "img_gb": img_gb,
    "img_reclaim_gb": img_reclaim_gb,
    "vms": vms,
    "fan": fan,
    "storage": storage,
    "parity_log": parity_log,
    "parity_cron": _pcron,
    "io_rate": io_rate,
    "net_rate": net_rate_h,
}

with open(tmp, "w") as f:
    json.dump(data, f)
os.replace(tmp, out)
PY
