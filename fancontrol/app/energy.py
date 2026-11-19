# -*- coding: utf-8 -*-
"""功耗采集与用电统计。

数据来源（优先级从高到低）
  1. RAPL —— /sys/class/powercap/intel-rapl:*，Intel 全系与 AMD zen2+ 可用，
     暴露 CPU 封装 / 内存的真实能耗计数器 energy_uj（单调递增，单位微焦耳）。
  2. 估算模型 —— 没有 RAPL 时按 CPU 占用率 + 硬盘数 + 网卡数推算。

口径（这里最容易算错，逐条写清楚）
  · package 域本身已经包含 core(PP0) 与 uncore(PP1)。三者相加会把核心重复计入，
    总功耗虚高一倍以上。CPU 部分 = package + dram（DRAM 是封装外的独立域），
    core / uncore 仅作分项展示。
  · RAPL 只覆盖 CPU 与内存，整机还要加主板 / 网卡 / 硬盘的底座功耗，
    否则 N150 空闲会显示成 2~3W 这种明显不对的数。
    整机 = package + dram + base_w + 全部硬盘功耗。
  · 硬盘按介质分：机械盘用配置值（默认 8W），SSD / NVMe 固定 2.5W。
    全闪机器统一按机械盘估会高估好几倍。

采样方式（与 fnmonitor 不同，我们是常驻进程，可以做得更准）
  fnmonitor 在单次 HTTP 请求里 sleep 1 秒读两次计数器，因为它按需出数；
  我们是守护进程，两次采样天然有间隔，直接用「本次 - 上次」的计数器差值：
      瓦数   = Δuj / Δt / 1e6
      电量   = Δuj / 3.6e12       （微焦耳 → kWh，精确值，不经过瓦数再乘时间）
  既不用 sleep，精度还更高。

电量归属
  按本地时区分「天」和「小时」两个维度的桶：
      days[YYYY-MM-DD].kwh[0..23]   该小时耗电量（kWh）
      days[YYYY-MM-DD].sec[0..23]   该小时有数据的秒数（算平均功率、判断数据完整性）
  小时桶是分时电价的基础 —— 每个小时整段落在同一个电价时段里，按桶计费不会有边界误差。
"""

import json
import math
import os
import re
import time

import ups

# 路径全部可用环境变量覆盖，便于在非 Linux 环境跑测试
PKGVAR = os.environ.get("FAN_PKGVAR", "/var/apps/FanControl/var")
DATA_FILE = os.path.join(PKGVAR, "energy.json")

RAPL_BASE = os.environ.get("FAN_RAPL_BASE", "/sys/class/powercap")
PROC_STAT = os.environ.get("FAN_PROC_STAT", "/proc/stat")
SYS_BLOCK = os.environ.get("FAN_SYS_BLOCK", "/sys/block")
SYS_NET = os.environ.get("FAN_SYS_NET", "/sys/class/net")
PROC_NET_DEV = os.environ.get("FAN_PROC_NET_DEV", "/proc/net/dev")

KEEP_DAYS = 45          # 数据保留天数（够算整月 + 余量）
SSD_TYPICAL_W = 2.5     # SSD / NVMe 单盘典型功耗
MAX_PERIODS = 12        # 分时电价最多段数

DEFAULT_POWER = {
    "rate_yuan": 0.6,        # 基础电价（元 / kWh）
    "price_periods": [],     # [{"start":"08:00","end":"22:00","rate":1.05}, ...]
    "tdp_w": 65,             # 估算模型的 CPU TDP
    "disk_typical_w": 8,     # 机械盘单盘功耗
    "nic_fixed_w": 5,        # 单块活动网卡功耗
    "base_w": 8,             # 主板 / 供电等固定底座（仅 RAPL 路径使用）
    "source": "auto",        # auto / ups / rapl / estimate
    "ups": {"host": "", "port": 0, "name": ""},
}

SOURCES = ("auto", "ups", "rapl", "estimate")

# 进程内状态：RAPL 上次读数、CPU 上次快照、上次采样时间
_RAPL = {"ts": 0.0, "vals": {}, "ranges": {}}
_CPU = {"total": 0, "idle": 0}
_LAST = {"ts": 0.0, "power_w": 0.0, "source": "unknown", "detail": {}}

# 实时读数快照文件。
#
# 为什么需要它：采样跑在 fand 守护进程里，页面数据由 server 进程响应，
# 两个进程各自 import 本模块 —— _LAST 是进程内变量，内存不共享。
# 不落盘的话，页面拿到的永远是 _LAST 的初始值（0 W / unknown），
# 于是「一直等待首次采样」，还错标成「模型估算」。
LIVE_FILE = os.path.join(PKGVAR, "energy_live.json")
LIVE_MAX_AGE = 120      # 超过这个秒数没有新快照就算「采样已停止」（另有一条按采样间隔算的）

_EMPTY_LAST = {"ts": 0.0, "power_w": 0.0, "source": "unknown", "detail": {}, "ups": {}}


# ---------------------------------------------------------------------------
# 配置规范化
# ---------------------------------------------------------------------------
def _finite(v, default, lo, hi):
    try:
        f = float(v)
        if not math.isfinite(f):
            return float(default)
    except (TypeError, ValueError):
        return float(default)
    return max(lo, min(hi, f))


def _hhmm_minutes(v):
    """'08:30' → 510；非法返回 None。"""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(v or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59 or (h == 24 and mi):
        return None
    return h * 60 + mi


def sanitize_periods(raw):
    """清洗分时电价时段：非法项直接丢弃，最多保留 MAX_PERIODS 段。

    start > end 表示跨零点（如 23:00–07:00）。start == end 视为非法。
    """
    out = []
    for it in (raw if isinstance(raw, list) else []):
        if not isinstance(it, dict):
            continue
        s, e = _hhmm_minutes(it.get("start")), _hhmm_minutes(it.get("end"))
        if s is None or e is None or s == e:
            continue
        # 电价非法（缺省 / 非数字 / NaN）整段丢弃，不能回落成 0 元 ——
        # 那等于给用户凭空免掉一段电费，比不填还糟
        try:
            rate = float(it.get("rate"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(rate):
            continue
        out.append({
            "start": "%02d:%02d" % (s // 60, s % 60),
            "end": "%02d:%02d" % (e // 60, e % 60),
            "rate": round(max(0.0, min(100.0, rate)), 4),
        })
        if len(out) >= MAX_PERIODS:
            break
    return out


def conf(cfg):
    """从应用配置里取出并规范化功耗段。"""
    m = (cfg or {}).get("metrics") or {}
    p = m.get("power") or {}
    src = str(p.get("source") or "auto").strip().lower()
    if src not in SOURCES:
        src = "auto"
    u = p.get("ups") if isinstance(p.get("ups"), dict) else {}
    return {
        "enabled": bool(m.get("enabled", True)),
        "interval": int(_finite(m.get("interval"), 30, 10, 600)),
        "rate_yuan": round(_finite(p.get("rate_yuan"), 0.6, 0.0, 20.0), 4),
        "price_periods": sanitize_periods(p.get("price_periods")),
        "tdp_w": round(_finite(p.get("tdp_w"), 65, 1, 1000), 1),
        "disk_typical_w": round(_finite(p.get("disk_typical_w"), 8, 0, 60), 1),
        "nic_fixed_w": round(_finite(p.get("nic_fixed_w"), 5, 0, 60), 1),
        "base_w": round(_finite(p.get("base_w"), 8, 0, 120), 1),
        "source": src,
        "ups": {
            "host": str(u.get("host") or "").strip()[:128],
            "port": int(_finite(u.get("port"), 0, 0, 65535)),
            "name": str(u.get("name") or "").strip()[:64],
        },
    }


# ---------------------------------------------------------------------------
# 电价
# ---------------------------------------------------------------------------
def period_rate(minute, periods, base_rate):
    """按一天内的分钟数（0–1439）匹配电价，未命中返回基础电价。"""
    for p in periods or []:
        s, e = _hhmm_minutes(p.get("start")), _hhmm_minutes(p.get("end"))
        if s is None or e is None or s == e:
            continue
        inside = (s <= minute < e) if s < e else (minute >= s or minute < e)
        if inside:
            try:
                return float(p.get("rate") or 0.0)
            except (TypeError, ValueError):
                return base_rate
    return base_rate


def rate_at(ts, periods, base_rate):
    """某一时刻适用的电价（按本地时区匹配）。"""
    lt = time.localtime(ts)
    return period_rate(lt.tm_hour * 60 + lt.tm_min, periods, base_rate)


def day_cost(hours, periods, base_rate):
    """一天的电费：24 个小时桶各自按该小时的电价计费。

    用小时桶而不是逐采样点算，是因为桶天然对齐电价时段边界，
    不会出现「07:59 的采样按 8 点后的电价算」这种偏移。
    """
    cost = 0.0
    for h in range(24):
        v = (hours[h] if h < len(hours) else 0.0) or 0.0
        if v > 0:
            cost += v * period_rate(h * 60, periods, base_rate)
    return cost


# ---------------------------------------------------------------------------
# 硬件读数
# ---------------------------------------------------------------------------
def _read_int(path):
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _read_text(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""


def _rapl_name(path):
    try:
        with open(os.path.join(path, "name"), "r") as f:
            return f.read().strip().lower()
    except Exception:
        return ""


def rapl_domains():
    """返回 {域目录名: (路径, 角色)}。角色 ∈ package / dram / core / uncore。"""
    out = {}
    try:
        names = sorted(os.listdir(RAPL_BASE))
    except Exception:
        return out
    for n in names:
        if not n.startswith("intel-rapl:"):
            continue
        p = os.path.join(RAPL_BASE, n)
        if not os.path.isfile(os.path.join(p, "energy_uj")):
            continue
        nm = _rapl_name(p)
        if nm.startswith("package"):
            role = "package"
        elif nm == "dram":
            role = "dram"
        elif nm in ("core", "uncore"):
            role = nm
        else:
            continue
        out[n] = (p, role)
    return out


def rapl_delta():
    """读一次 RAPL 计数器，返回相对上次的增量与平均功率。

    返回 None 表示：没有 RAPL、或这是首次采样（没有基线）。
    返回 dict：{"dt": 秒, "uj": {角色: 微焦耳增量}, "w": {角色: 瓦}}
    """
    domains = rapl_domains()
    if not domains:
        return None

    now = time.monotonic()
    cur, ranges = {}, {}
    for n, (p, _role) in domains.items():
        v = _read_int(os.path.join(p, "energy_uj"))
        if v is None:
            continue
        cur[n] = v
        r = _read_int(os.path.join(p, "max_energy_range_uj"))
        if r:
            ranges[n] = r

    prev_vals, prev_ts = _RAPL["vals"], _RAPL["ts"]
    _RAPL["vals"], _RAPL["ts"] = cur, now
    if ranges:
        _RAPL["ranges"].update(ranges)
    if not prev_vals or prev_ts <= 0 or not cur:
        return None

    dt = now - prev_ts
    if dt <= 0:
        return None

    uj = {}
    for n, v in cur.items():
        if n not in prev_vals:
            continue
        d = v - prev_vals[n]
        if d < 0:
            # 计数器回绕（max_energy_range_uj 是回绕上限）
            top = ranges.get(n) or _RAPL["ranges"].get(n)
            if not top:
                continue
            d += top
        role = domains[n][1]
        uj[role] = uj.get(role, 0) + d

    if not uj:
        return None
    watts = {k: (v / dt / 1e6) for k, v in uj.items()}
    return {"dt": dt, "uj": uj, "w": watts}


def cpu_percent():
    """从 /proc/stat 算两次采样之间的 CPU 占用率（含 iowait 计为空闲）。"""
    line = _read_text(PROC_STAT).splitlines()
    if not line:
        return None
    parts = line[0].split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        return None
    try:
        vals = [int(x) for x in parts[1:9]]
    except (TypeError, ValueError):
        return None
    total = sum(vals)
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)

    prev_total, prev_idle = _CPU["total"], _CPU["idle"]
    _CPU["total"], _CPU["idle"] = total, idle
    if not prev_total:
        return None
    dt = total - prev_total
    di = idle - prev_idle
    if dt <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - float(di) / dt) * 100.0))


def list_blocks():
    """物理整盘列表（排除 loop / ram / zram / dm / md / sr 与分区）。"""
    out = []
    try:
        names = os.listdir(SYS_BLOCK)
    except Exception:
        return out
    for n in sorted(names):
        if n.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "fd")):
            continue
        p = os.path.join(SYS_BLOCK, n)
        # 有 device 链接才是真实设备；分区不在 /sys/block 下，无需额外过滤
        if not os.path.exists(os.path.join(p, "device")):
            continue
        out.append(n)
    return out


def is_ssd(name):
    """按 rotational 判断介质：0 = 固态。读不到按机械盘处理（与旧行为一致）。"""
    if str(name).startswith("nvme"):
        return True
    v = _read_text(os.path.join(SYS_BLOCK, name, "queue", "rotational")).strip()
    return v == "0"


def disk_watts(cfg):
    """全部物理盘的功耗合计（按介质区分）。返回 (瓦, 盘数)。"""
    blocks = list_blocks()
    hdd_w = float(cfg.get("disk_typical_w") or 8)
    total = 0.0
    for b in blocks:
        total += SSD_TYPICAL_W if is_ssd(b) else hdd_w
    return round(total, 2), len(blocks)


def active_nics(exclude_virt=True):
    """活动网口数（有 carrier 或 operstate=up），排除 lo 与虚拟网卡。"""
    virt = ("docker", "veth", "br-", "virbr", "tun", "tap", "vnet", "lxc", "kube", "wg")
    try:
        names = os.listdir(SYS_NET)
    except Exception:
        return 0
    n = 0
    for name in names:
        if name == "lo":
            continue
        if exclude_virt and name.startswith(virt):
            continue
        state = _read_text(os.path.join(SYS_NET, name, "operstate")).strip()
        if state == "up":
            n += 1
    return n


# ---------------------------------------------------------------------------
# 估算模型（无 RAPL 时回退）
# ---------------------------------------------------------------------------
def estimate_watts(cpu_pct, disks_w, nics, cfg):
    """无硬件功耗传感器时的整机估算（瓦）。

    公式：TDP × (占用率 × 0.7 + 0.3) + 硬盘功耗 + 网卡数 × 单网卡功耗
    系数 0.7 / 0.3 的意思是：即使 0 负载，CPU 也要吃 30% TDP（漏电 + 基础供电）。
    """
    tdp = float(cfg.get("tdp_w") or 65)
    cpu = float(cpu_pct or 0.0)
    base = tdp * (cpu / 100.0 * 0.7 + 0.3)
    return round(base + disks_w + nics * float(cfg.get("nic_fixed_w") or 5), 2)


# ---------------------------------------------------------------------------
# 数据文件
# ---------------------------------------------------------------------------
def _today_key(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _blank_day():
    return {"kwh": [0.0] * 24, "sec": [0.0] * 24}


def load_data():
    data = {}
    if os.path.isfile(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    days = data.get("days")
    if not isinstance(days, dict):
        days = {}
    # 结构修补：旧版本 / 手改坏了的文件不要让整页崩掉
    out = {}
    for d, v in days.items():
        if not isinstance(v, dict):
            continue
        kwh = v.get("kwh")
        sec = v.get("sec")
        rec = _blank_day()
        if isinstance(kwh, list):
            for i in range(min(24, len(kwh))):
                try:
                    rec["kwh"][i] = max(0.0, float(kwh[i]))
                except (TypeError, ValueError):
                    pass
        if isinstance(sec, list):
            for i in range(min(24, len(sec))):
                try:
                    rec["sec"][i] = max(0.0, float(sec[i]))
                except (TypeError, ValueError):
                    pass
        out[d] = rec
    return {"v": 1, "days": out}


def save_data(data):
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, DATA_FILE)
        return True
    except Exception:
        return False


def prune(data, keep_days=KEEP_DAYS):
    """删掉太老的日期，别让文件无限长。"""
    cutoff = _today_key(time.time() - keep_days * 86400)
    days = data.get("days") or {}
    for d in list(days.keys()):
        if d < cutoff:
            del days[d]
    return data


# ---------------------------------------------------------------------------
# 实时读数（跨进程）
# ---------------------------------------------------------------------------
def _write_live():
    """把本次读数落盘，供另一个进程（Web 服务）读取。失败不影响采样。"""
    try:
        os.makedirs(PKGVAR, exist_ok=True)
        tmp = LIVE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_LAST, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, LIVE_FILE)
        return True
    except Exception:
        return False


def _read_live():
    """取实时读数：本进程采过样就用自己的，否则读守护进程留下的快照。

    内存优先是有意的 —— 同进程测试与「刚采完立刻查」都走这条路，
    省一次磁盘读；跨进程时内存一定是空的，自然回落到文件。
    """
    if _LAST["ts"] > 0:
        return _LAST
    try:
        with open(LIVE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return dict(_EMPTY_LAST)
    if not isinstance(raw, dict):
        return dict(_EMPTY_LAST)
    out = dict(_EMPTY_LAST)
    try:
        out["ts"] = float(raw.get("ts") or 0)
        out["power_w"] = float(raw.get("power_w") or 0)
    except (TypeError, ValueError):
        pass
    src = raw.get("source")
    if isinstance(src, str):
        out["source"] = src
    det = raw.get("detail")
    if isinstance(det, dict):
        out["detail"] = det
    u = raw.get("ups")
    if isinstance(u, dict):
        out["ups"] = u
    return out


def _clear_live():
    try:
        os.remove(LIVE_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------
def sample(cfg):
    """采样一次并累积到小时桶，返回本次读数（供页面即时显示）。

    第一次调用只建立基线（RAPL 需要两次读数才有差值），不累积电量。
    """
    c = conf(cfg)
    now = time.time()
    prev_ts = _LAST["ts"]
    first = prev_ts <= 0
    gap = (now - prev_ts) if not first else 0.0

    # 断档处理：机器关机 / 休眠 / 进程被挂起时，两次采样可能隔了几个小时。
    # 按一个采样间隔计，否则会把关机那段时间也算成用电。
    interval = c["interval"]
    dt = gap if (not first and 0 < gap <= max(3 * interval, interval + 30)) else (interval if not first else 0)

    cpu_pct = cpu_percent()
    disks_w, disks_n = disk_watts(c)
    nics = active_nics()

    # UPS 整机功率：插座级实测，含主板 / 硬盘 / 风扇 / 电源损耗。
    # 有读数就不再加底座与硬盘 —— 那些本来就在 UPS 的量里。
    ups_w, ups_info = None, {}
    if c["source"] in ("auto", "ups"):
        try:
            ups_w, ups_info = ups.read_power(c["ups"])
        except Exception as e:
            ups_info = {"error": str(e)}

    rd = rapl_delta()
    power_w = 0.0
    added_kwh = 0.0
    source = "unavailable"
    detail = {"disks": disks_n, "disks_w": disks_w, "cpu_percent": round(cpu_pct, 1) if cpu_pct is not None else None}

    if ups_w:
        power_w = float(ups_w)
        source = "ups"
        added_kwh = power_w * dt / 3600000.0
        detail.update({"ups_w": round(power_w, 2),
                       "ups_from": ups_info.get("from") or "",
                       "ups_load": ups_info.get("load"),
                       "ups_status": ups_info.get("status") or "",
                       "ups_model": ups_info.get("model") or "",
                       "ups_name": ups_info.get("name") or ""})
    elif rd and rd["uj"] and c["source"] in ("auto", "rapl"):
        pkg_w = float(rd["w"].get("package") or 0.0)
        dram_w = float(rd["w"].get("dram") or 0.0)
        base_w = float(c["base_w"])
        cpu_w = pkg_w + dram_w
        power_w = cpu_w + base_w + disks_w
        source = "rapl"
        detail.update({"package_w": round(pkg_w, 2), "dram_w": round(dram_w, 2),
                       "core_w": round(float(rd["w"].get("core") or 0.0), 2),
                       "uncore_w": round(float(rd["w"].get("uncore") or 0.0), 2),
                       "base_w": base_w, "cpu_w": round(cpu_w, 2)})
        # 电量：CPU 部分直接用计数器差值（精确），底座 + 硬盘部分按时间积分。
        # 单位换算别写错：1 kWh = 3.6e6 J = 3.6e12 µJ（写 3.6e9 会小 1000 倍）。
        cpu_kwh = (float(rd["uj"].get("package") or 0) + float(rd["uj"].get("dram") or 0)) / 3.6e12
        added_kwh = cpu_kwh + (base_w + disks_w) * dt / 3600000.0
    elif c["source"] == "ups":
        # 选了「只认 UPS」却没读到：不拿估算值凑数，明确标成不可用，
        # 免得用户以为读的是 UPS 的真实值。
        source = "unavailable"
        detail.update({"ups_error": ups_info.get("error") or "UPS 没有读数"})
    else:
        est = estimate_watts(cpu_pct, disks_w, nics, c)
        power_w = est
        source = "estimated"
        detail.update({"nics": nics})
        added_kwh = est * dt / 3600000.0

    power_w = round(power_w, 2)
    added_kwh = round(max(0.0, added_kwh), 6)

    if dt > 0 and added_kwh > 0:
        data = load_data()
        day = _today_key(now)
        rec = data["days"].get(day) or _blank_day()
        data["days"][day] = rec
        h = time.localtime(now).tm_hour
        rec["kwh"][h] = round(rec["kwh"][h] + added_kwh, 6)
        rec["sec"][h] = round(rec["sec"][h] + dt, 3)
        save_data(prune(data))

    _LAST.update({"ts": now, "power_w": power_w, "source": source,
                  "detail": detail, "ups": ups_info})
    _write_live()
    return {"ok": True, "power_w": power_w, "source": source,
            "added_kwh": added_kwh, "dt": round(dt, 1), "detail": detail}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
SOURCE_TEXT = {"ups": "UPS 实测", "rapl": "RAPL 实测", "estimated": "模型估算", "unavailable": "不可用"}


def _sum_range(days_map, from_key, periods, rate):
    kwh = 0.0
    cost = 0.0
    sec = 0.0
    for d, rec in days_map.items():
        if d < from_key:
            continue
        kwh += sum(rec["kwh"])
        sec += sum(rec["sec"])
        cost += day_cost(rec["kwh"], periods, rate)
    return kwh, cost, sec


def summary(cfg, days=30):
    """给页面用的汇总：实时读数 + 今日 / 本月 + 近 N 天。"""
    c = conf(cfg)
    data = load_data()
    days_map = data.get("days") or {}
    today = _today_key()
    month_prefix = today[:8]        # 'YYYY-MM-'

    today_rec = days_map.get(today) or _blank_day()
    today_kwh = sum(today_rec["kwh"])
    today_sec = sum(today_rec["sec"])
    today_cost = day_cost(today_rec["kwh"], c["price_periods"], c["rate_yuan"])

    month_kwh, month_cost, month_sec = _sum_range(
        days_map, month_prefix + "01", c["price_periods"], c["rate_yuan"])

    # 近 N 天序列（按日期升序，缺数据的日期补 null）
    series = []
    day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    for i in range(days - 1, -1, -1):
        ts = day_start - i * 86400
        key = _today_key(ts + 3600)
        rec = days_map.get(key)
        if rec is None:
            series.append({"day": key[5:], "kwh": None, "cost": None, "avg_w": None})
        else:
            k = sum(rec["kwh"])
            s = sum(rec["sec"])
            series.append({
                "day": key[5:],
                "kwh": round(k, 4),
                "cost": round(day_cost(rec["kwh"], c["price_periods"], c["rate_yuan"]), 2),
                "avg_w": round(k * 3600000.0 / s, 1) if s > 0 else None,
            })

    last = _read_live()
    now = time.time()
    age = (now - last["ts"]) if last["ts"] > 0 else None
    # 新鲜度：一个采样周期内（留 3 倍余量，且不低于 120 秒）才算「正在采集」。
    # 超过就是守护进程停了 / 开关被关了 / 采样本轮失败。
    fresh = age is not None and age <= max(3 * c["interval"], LIVE_MAX_AGE)
    has_val = last["source"] in ("ups", "rapl", "estimated")
    ok = bool(fresh and has_val)
    if not c["enabled"]:
        state = "off"
    elif last["ts"] <= 0:
        state = "waiting"        # 从没采过：守护进程没起来，或刚装好
    elif not fresh:
        state = "stale"          # 采过，但停了
    elif not has_val:
        state = "unknown"
    else:
        state = "ok"

    return {
        "enabled": c["enabled"],
        # ok 恒真：它只表示「接口算出来了」。有没有实时读数用 live / state 表达 ——
        # 前端把 ok=false 当请求失败（历史累计也会被一起吞掉）。
        "ok": True,
        "live": ok,
        "state": state,
        "age": round(age, 1) if age is not None else None,
        "source": last["source"],
        "source_text": SOURCE_TEXT.get(last["source"], "未知") if ok else "未采集",
        "power_w": last["power_w"] if ok else 0.0,
        "detail": last["detail"] if ok else {},
        "today": {"kwh": round(today_kwh, 4),
                  "cost": round(today_cost, 2),
                  "avg_w": round(today_kwh * 3600000.0 / today_sec, 1) if today_sec > 0 else None,
                  "cover_h": round(today_sec / 3600.0, 2)},
        "month": {"kwh": round(month_kwh, 4),
                  "cost": round(month_cost, 2),
                  "avg_w": round(month_kwh * 3600000.0 / month_sec, 1) if month_sec > 0 else None,
                  "cover_h": round(month_sec / 3600.0, 1)},
        "days": series,
        "rate": c["rate_yuan"],
        "periods": c["price_periods"],
        "params": {"tdp_w": c["tdp_w"], "disk_typical_w": c["disk_typical_w"],
                   "nic_fixed_w": c["nic_fixed_w"], "base_w": c["base_w"],
                   "source": c["source"], "ups": c["ups"]},
        # UPS 侧最近一次采样的结果（含失败原因），页面的「检测」按钮另有 /api/ups
        "ups": last.get("ups") or {},
        "ups_wanted": c["source"] in ("auto", "ups"),
        "interval": c["interval"],
        "last_sample": last["ts"] or None,
        "now": time.time(),
    }


def export_csv(cfg, days=30):
    """导出近 N 天逐日明细，返回 CSV 文本。"""
    c = conf(cfg)
    data = load_data()
    days_map = data.get("days") or {}
    rows = ["日期,用电kWh,电费元,平均功率W,覆盖小时"]
    day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    for i in range(days - 1, -1, -1):
        key = _today_key(day_start - i * 86400 + 3600)
        rec = days_map.get(key)
        if rec is None:
            rows.append("%s,,,," % key)
            continue
        k = sum(rec["kwh"])
        s = sum(rec["sec"])
        cost = day_cost(rec["kwh"], c["price_periods"], c["rate_yuan"])
        rows.append("%s,%.4f,%.2f,%s,%.2f"
                    % (key, k, cost, ("%.1f" % (k * 3600000.0 / s)) if s > 0 else "", s / 3600.0))
    return "\n".join(rows) + "\n"


def reset():
    """清空历史（页面「清空统计」用）。"""
    save_data({"v": 1, "days": {}})
    _clear_live()
    return True
