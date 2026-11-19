# -*- coding: utf-8 -*-
"""网络流量统计。

数据来源
  /proc/net/dev —— 每块网卡的累计收发字节（内核开机以来单调递增，开机归零）。

统计口径（决定了数字准不准）
  · 排除 lo：环回流量不是真实网络流量。
  · 默认排除虚拟网卡（docker / veth / br- / virbr / tun / tap / vnet / lxc / kube / wg）：
    容器流量在 veth 与物理网卡上各计一次，不排除会翻倍。可在设置里关掉这个排除。
  · 「开机累计」直接用计数器当前值 —— /proc/net/dev 本来就是开机以来累计。
  · 「今日 / 本月」用两次采样的差值累加进日桶，不依赖计数器任何时候归零：
    网卡重置 / docker 网络重建会让计数器变小，此时按「新值即增量」处理
    （与 Prometheus 的 increase() 同思路），避免出现负数。

为什么不用 SQLite
  需求只有「今日 / 本月 / 近 30 天」，日级桶最多 45 条记录，
  一个 JSON 文件就够了，也符合本应用「零依赖 + 纯文件」的既有风格。
"""

import json
import os
import socket
import time

PKGVAR = os.environ.get("FAN_PKGVAR", "/var/apps/FanControl/var")
DATA_FILE = os.path.join(PKGVAR, "netflow.json")

# 实时状态快照（网卡计数器 + 速率）。与 energy.py 同理：采样在 fand 进程，
# 页面在 server 进程，内存里的 _STATE 跨不过去，不落盘就永远拿不到实时速率。
STATE_FILE = os.path.join(PKGVAR, "netflow_live.json")
LIVE_MAX_AGE = 120

PROC_NET_DEV = os.environ.get("FAN_PROC_NET_DEV", "/proc/net/dev")
DOCKER_SOCK = os.environ.get("FAN_DOCKER_SOCK", "/var/run/docker.sock")

KEEP_DAYS = 45
VIRT_PREFIXES = ("docker", "veth", "br-", "virbr", "tun", "tap", "vnet", "lxc", "kube", "wg")

# 进程内状态
_STATE = {"ts": 0.0, "dev": {}, "rates": {}}
_CONTAINERS = {"ts": 0.0, "data": []}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def conf(cfg):
    m = (cfg or {}).get("metrics") or {}
    t = m.get("traffic") or {}
    try:
        interval = int(m.get("interval") or 30)
    except (TypeError, ValueError):
        interval = 30
    return {
        "enabled": bool(m.get("enabled", True)),
        "interval": max(10, min(600, interval)),
        "exclude_bridge": bool(t.get("exclude_bridge", True)),
        "with_containers": bool(t.get("containers", True)),
    }


def is_virtual(name):
    return str(name).startswith(VIRT_PREFIXES)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def read_net_dev():
    """解析 /proc/net/dev，返回 {网卡: {"rx": 字节, "tx": 字节, "rx_pkt":, "tx_pkt":}}。"""
    out = {}
    try:
        with open(PROC_NET_DEV, "r") as f:
            text = f.read()
    except Exception:
        return out
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if not name or len(fields) < 16:
            continue
        try:
            out[name] = {
                "rx": int(fields[0]),
                "rx_pkt": int(fields[1]),
                "rx_err": int(fields[2]),
                "rx_drop": int(fields[3]),
                "tx": int(fields[8]),
                "tx_pkt": int(fields[9]),
                "tx_err": int(fields[10]),
                "tx_drop": int(fields[11]),
            }
        except (TypeError, ValueError):
            continue
    return out


def _read_text(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""


def _sum_iface_counters(text):
    """从一段 /proc/net/dev 格式的文本里汇总非 lo 网卡的收发字节。"""
    rx = tx = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        if name.strip() in ("", "lo", "Inter-|", "face"):
            continue
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except (TypeError, ValueError):
            continue
    return rx, tx


# ---------------------------------------------------------------------------
# 容器流量（可选，读 docker.sock）
# ---------------------------------------------------------------------------
def _docker_get(path, timeout=3.0):
    """通过 unix socket 发一个最小 HTTP/1.0 请求（零依赖，不引入 docker SDK）。"""
    if not os.path.exists(DOCKER_SOCK):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(DOCKER_SOCK)
        req = ("GET " + path + " HTTP/1.0\r\nHost: docker\r\n"
               "Connection: close\r\nAccept: application/json\r\n\r\n")
        s.sendall(req.encode("utf-8"))
        chunks = []
        total = 0
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > 4 * 1024 * 1024:      # 防护：别被异常响应撑爆内存
                break
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0] if head else b""
    if b" 200 " not in status:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None


def container_traffic(ttl=60):
    """运行中容器的本次运行累计流量，按总量降序。没有 docker.sock 返回空列表。

    读的是 /proc/<pid>/net/dev —— 那是容器自身网络命名空间的计数器，
    容器停止即消失，所以这里的「累计」是「本次运行以来」。
    """
    now = time.time()
    if _CONTAINERS["ts"] and now - _CONTAINERS["ts"] < ttl:
        return _CONTAINERS["data"]

    if not os.path.exists(DOCKER_SOCK):
        _CONTAINERS["data"] = []
        _CONTAINERS["ts"] = now
        return []

    listing = _docker_get("/containers/json")
    if not isinstance(listing, list):
        _CONTAINERS["data"] = []
        _CONTAINERS["ts"] = now
        return []

    out = []
    for c in listing:
        if not isinstance(c, dict):
            continue
        cid = c.get("Id") or ""
        if not cid:
            continue
        names = c.get("Names") or []
        name = (names[0].lstrip("/") if names else cid[:12])
        detail = _docker_get("/containers/%s/json" % cid)
        pid = None
        if isinstance(detail, dict):
            pid = ((detail.get("State") or {}).get("Pid"))
        if not pid:
            continue
        text = _read_text("/proc/%s/net/dev" % pid)
        if not text:
            continue
        rx, tx = _sum_iface_counters(text)
        out.append({"name": name, "rx": rx, "tx": tx, "total": rx + tx,
                    "image": c.get("Image") or ""})
    out.sort(key=lambda x: -x["total"])
    _CONTAINERS["data"] = out[:15]
    _CONTAINERS["ts"] = now
    return _CONTAINERS["data"]


# ---------------------------------------------------------------------------
# 数据文件
# ---------------------------------------------------------------------------
def _today_key(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


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
    out = {}
    for d, v in days.items():
        if not isinstance(v, dict):
            continue
        rec = {}
        for k in ("rx", "tx", "sec"):
            try:
                rec[k] = max(0.0, float(v.get(k) or 0))
            except (TypeError, ValueError):
                rec[k] = 0.0
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
    cutoff = _today_key(time.time() - keep_days * 86400)
    days = data.get("days") or {}
    for d in list(days.keys()):
        if d < cutoff:
            del days[d]
    return data


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------
def _eligible(name, exclude_bridge):
    if name == "lo":
        return False
    if exclude_bridge and is_virtual(name):
        return False
    return True


def _write_state():
    """把本次采样的计数器与速率落盘，供 server 进程读取。"""
    try:
        os.makedirs(PKGVAR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, STATE_FILE)
        return True
    except Exception:
        return False


def _read_state():
    """取实时状态：本进程采过样用自己的，否则读守护进程留下的快照。"""
    if _STATE["ts"] > 0:
        return _STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {"ts": 0.0, "dev": {}, "rates": {}}
    if not isinstance(raw, dict):
        return {"ts": 0.0, "dev": {}, "rates": {}}
    out = {"ts": 0.0, "dev": {}, "rates": {}}
    try:
        out["ts"] = float(raw.get("ts") or 0)
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("dev"), dict):
        out["dev"] = raw["dev"]
    if isinstance(raw.get("rates"), dict):
        out["rates"] = raw["rates"]
    return out


def sample(cfg):
    """采样一次：算各网卡速率，并把增量累加进日桶。"""
    c = conf(cfg)
    now = time.time()
    cur = read_net_dev()

    prev = _STATE["dev"]
    prev_ts = _STATE["ts"]
    first = not prev or prev_ts <= 0
    gap = (now - prev_ts) if not first else 0.0
    cap = max(3 * c["interval"], c["interval"] + 30)
    # 断档（关机 / 休眠）时按一个采样间隔计，避免把停机时间算成流量
    dt = gap if (not first and 0 < gap <= cap) else (c["interval"] if not first else 0)

    rates = {}
    if not first and dt > 0:
        for name, v in cur.items():
            p = prev.get(name)
            if not p:
                continue
            rates[name] = {
                "rx": max(0.0, (v["rx"] - p["rx"]) / dt),
                "tx": max(0.0, (v["tx"] - p["tx"]) / dt),
            }

    rx_delta = tx_delta = 0
    if not first and dt > 0:
        for name, v in cur.items():
            if not _eligible(name, c["exclude_bridge"]):
                continue
            p = prev.get(name)
            if p is None:
                continue          # 新出现的网卡：本次只建基线
            drx = v["rx"] - p["rx"]
            dtx = v["tx"] - p["tx"]
            # 计数器归零（网卡重置 / 容器网络重建）→ 按新值即增量
            rx_delta += drx if drx >= 0 else v["rx"]
            tx_delta += dtx if dtx >= 0 else v["tx"]

        if rx_delta or tx_delta:
            data = load_data()
            day = _today_key(now)
            rec = data["days"].get(day) or {"rx": 0.0, "tx": 0.0, "sec": 0.0}
            data["days"][day] = rec
            rec["rx"] = round(rec["rx"] + rx_delta, 1)
            rec["tx"] = round(rec["tx"] + tx_delta, 1)
            rec["sec"] = round(rec["sec"] + dt, 3)
            save_data(prune(data))

    _STATE["dev"] = cur
    _STATE["ts"] = now
    _STATE["rates"] = rates
    _write_state()
    return {"ok": True, "dt": round(dt, 1), "rx": rx_delta, "tx": tx_delta,
            "ifaces": len(cur)}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%.2f TB" % n


def summary(cfg, days=30):
    c = conf(cfg)
    data = load_data()
    days_map = data.get("days") or {}
    today = _today_key()
    month_prefix = today[:8]

    total_rx = total_tx = 0.0
    today_rx = today_tx = 0.0
    month_rx = month_tx = 0.0
    today_sec = 0.0
    for d, rec in days_map.items():
        if d == today:
            today_rx += rec["rx"]
            today_tx += rec["tx"]
            today_sec += rec["sec"]
        if d >= month_prefix + "01":
            month_rx += rec["rx"]
            month_tx += rec["tx"]

    # 各网卡：实时速率 + 开机累计
    st = _read_state()
    now = time.time()
    age = (now - st["ts"]) if st["ts"] > 0 else None
    fresh = age is not None and age <= max(3 * c["interval"], LIVE_MAX_AGE)
    # 采样停了就别拿几十秒前的速率冒充「实时」；计数器本身仍然实时可读，
    # 所以「开机累计」照常显示，只是速率一栏留空。
    cur = st["dev"] if (fresh and st["dev"]) else read_net_dev()
    rates = st["rates"] if fresh else {}
    ifaces = []
    for name, v in cur.items():
        if name == "lo":
            continue
        r = rates.get(name) or {"rx": 0.0, "tx": 0.0}
        ifaces.append({
            "name": name,
            "rx_bps": round(r["rx"], 1),
            "tx_bps": round(r["tx"], 1),
            "rx_total": v["rx"],
            "tx_total": v["tx"],
            "virtual": is_virtual(name),
            "counted": _eligible(name, c["exclude_bridge"]),
        })
    ifaces.sort(key=lambda x: -(x["rx_bps"] + x["tx_bps"]))

    counted = [i for i in ifaces if i["counted"]]
    boot_rx = sum(i["rx_total"] for i in counted)
    boot_tx = sum(i["tx_total"] for i in counted)
    rt_rx = sum(i["rx_bps"] for i in counted)
    rt_tx = sum(i["tx_bps"] for i in counted)
    total_rx = boot_rx
    total_tx = boot_tx

    series = []
    day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    for i in range(days - 1, -1, -1):
        key = _today_key(day_start - i * 86400 + 3600)
        rec = days_map.get(key)
        series.append({
            "day": key[5:],
            "rx": rec["rx"] if rec else None,
            "tx": rec["tx"] if rec else None,
        })

    containers = container_traffic() if c["with_containers"] else []

    return {
        "enabled": c["enabled"],
        # ok 只表示「这次调用算出来了」。历史累计即便当前读不到网卡也是有价值的，
        # 所以「没有实时计数器」单独用 live 表达，别混进 ok —— 前端把 ok=false 当请求失败。
        "ok": True,
        "live": bool(fresh and cur),
        "state": ("off" if not c["enabled"] else
                  "waiting" if st["ts"] <= 0 else
                  "stale" if not fresh else "ok"),
        "age": round(age, 1) if age is not None else None,
        "last_sample": st["ts"] or None,
        "realtime": {"rx_bps": round(rt_rx, 1), "tx_bps": round(rt_tx, 1)},
        "ifaces": ifaces,
        "totals": {
            "boot_rx": boot_rx, "boot_tx": boot_tx,
            "today_rx": round(today_rx, 1), "today_tx": round(today_tx, 1),
            "month_rx": round(month_rx, 1), "month_tx": round(month_tx, 1),
            "today_cover_h": round(today_sec / 3600.0, 2),
        },
        "days": series,
        "containers": containers,
        "container_supported": os.path.exists(DOCKER_SOCK),
        "exclude_bridge": c["exclude_bridge"],
        "interval": c["interval"],
        "now": time.time(),
    }


def export_csv(cfg, days=30):
    """导出近 N 天逐日流量，返回 CSV 文本。"""
    data = load_data()
    days_map = data.get("days") or {}
    rows = ["日期,下行字节,上行字节,下行,上行,覆盖小时"]
    day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    for i in range(days - 1, -1, -1):
        key = _today_key(day_start - i * 86400 + 3600)
        rec = days_map.get(key)
        if rec is None:
            rows.append("%s,,,,," % key)
            continue
        rows.append("%s,%d,%d,%s,%s,%.2f"
                    % (key, int(rec["rx"]), int(rec["tx"]),
                       fmt_bytes(rec["rx"]), fmt_bytes(rec["tx"]), rec["sec"] / 3600.0))
    return "\n".join(rows) + "\n"


def reset():
    save_data({"v": 1, "days": {}})
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass
    return True
