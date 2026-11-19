# -*- coding: utf-8 -*-
"""UPS 功率读取（NUT 协议，只读）。

为什么要接 UPS
  RAPL 只能测到 CPU 封装 + 内存，整机还要靠底座与硬盘估值，全闪机器差个几瓦、
  带机械盘差十几瓦都正常。UPS 报的功率是**插座级实测**，包含主板、硬盘、风扇、
  网卡、电源自身损耗，是最接近电表的口径。所以只要读得到就优先用它。

怎么读
  直接用 TCP 说 NUT 协议，不调 upsc 命令：
      LIST UPS                → 实例名列表
      LIST VAR <实例名>       → 全部变量
  这样不依赖 upsc 是否安装、在不在 PATH、有没有执行权限，也不受 fnOS
  升级后命令路径变化影响。只读，不发任何 SET/INSTCMD，不接管 UPS。

  注意 UPS 的 USB 那层不归我们管：飞牛的「UPS 电源管理」已经把 usbhid-ups
  跑起来了，我们只是去问它背后的 upsd（默认 127.0.0.1:3493）。

功率取哪个变量（各家驱动差异很大，按可靠性排序）
  1. ups.realpower / output.power / output.L1.power —— 设备直报的有功功率，最准
  2. ups.power —— 多数机型是有功功率，少部分是视在功率（VA），仍优于估算
  3. 输出电压 × 输出电流 —— 瓦力方程这类直流输出机型只有这一条路
     （样本：19.13V × 1.365A = 26.1W）
  4. ups.load(%) × 额定容量 —— 兜底，误差大，仅供参考

  读到的值必须落在 (0, 5000] 瓦才认。市电切换瞬间有 0V 假帧，
  直接丢掉比记一笔 0 元电费强。
"""

import math
import os
import socket
import time

NUT_HOST = os.environ.get("FAN_NUT_HOST", "")       # 空 = 自动探测
NUT_PORT = int(os.environ.get("FAN_NUT_PORT") or 0)
NUT_NAME = os.environ.get("FAN_NUT_NAME", "")

DEFAULT_PORT = 3493
CONNECT_TIMEOUT = 1.5      # 探测要快，不能把采样线程拖住
READ_TIMEOUT = 2.5
MAX_WATT = 5000.0

# 探测结果缓存：连哪个地址、实例叫什么，这两件事基本不变。
# 功率值每次都现读，不缓存。
_TARGET = {"key": "", "host": "", "port": 0, "name": "", "ts": 0.0, "err": ""}
_TARGET_TTL_OK = 600.0
_TARGET_TTL_FAIL = 120.0   # 探测失败别每轮都白等超时（没接 UPS 是常态）


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
def _recv_list(f, cmd):
    """发一条 LIST 命令，读到 END LIST 为止，返回行列表（不含首尾标记）。"""
    f.write((cmd + "\n").encode("utf-8"))
    f.flush()
    out = []
    while True:
        raw = f.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("BEGIN LIST"):
            continue
        if line.startswith("END LIST"):
            break
        if line.startswith("ERR"):
            raise RuntimeError(line[4:].strip() or "ERR")
        out.append(line)
    return out


def _query(host, port, cmds):
    """连一次，按顺序执行多条 LIST 命令，返回 {命令: 行列表}。"""
    s = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    try:
        s.settimeout(READ_TIMEOUT)
        f = s.makefile("rwb")
        try:
            return {c: _recv_list(f, c) for c in cmds}
        finally:
            try:
                f.close()
            except Exception:
                pass
    finally:
        try:
            s.close()
        except Exception:
            pass


def _parse_vars(lines, name):
    """'VAR ups battery.charge "100"' → {'battery.charge': '100'}"""
    out = {}
    for line in lines:
        if not line.startswith("VAR "):
            continue
        # 变量值里可能有空格，所以按前两个空白切，剩下的整段去掉首尾引号
        parts = line.split(" ", 3)
        if len(parts) < 4:
            continue
        key, val = parts[2], parts[3].strip()
        if not (key and val):
            continue
        if val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        out[key] = val
    return out


def list_ups(host, port):
    """返回 [(实例名, 描述)]。"""
    res = _query(host, port, ["LIST UPS"])["LIST UPS"]
    out = []
    for line in res:
        if not line.startswith("UPS "):
            continue
        rest = line[4:]
        name = rest.split(" ", 1)[0]
        desc = ""
        i = rest.find('"')
        if i >= 0:
            desc = rest[i:].strip().strip('"')
        if name:
            out.append((name, desc))
    return out


def read_vars(host, port, name):
    return _parse_vars(_query(host, port, ["LIST VAR %s" % name])["LIST VAR %s" % name], name)


# ---------------------------------------------------------------------------
# 目标探测
# ---------------------------------------------------------------------------
def _upsd_conf_hosts():
    """从 /etc/nut/upsd.conf 里捞 LISTEN 地址 —— 有人把端口改成非默认值。"""
    out = []
    path = os.environ.get("FAN_UPSD_CONF", "/etc/nut/upsd.conf")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0].upper() == "LISTEN":
                    addr = parts[1]
                    # LISTEN 127.0.0.1 3493 或 LISTEN 127.0.0.1
                    port = DEFAULT_PORT
                    if len(parts) >= 3 and parts[2].isdigit():
                        port = int(parts[2])
                    out.append((addr, port))
    except Exception:
        pass
    return out


def candidates(cfg=None):
    """按优先级列出要试的 (host, port)。"""
    c = (cfg or {})
    out = []
    host = (c.get("host") or NUT_HOST or "").strip()
    if host:
        port = int(c.get("port") or NUT_PORT or DEFAULT_PORT)
        out.append((host, port))
        return out
    if NUT_PORT:
        out.append(("127.0.0.1", NUT_PORT))
    for a, p in _upsd_conf_hosts():
        # LISTEN 0.0.0.0 是「所有地址」，本机连 127.0.0.1 就行
        if a in ("0.0.0.0", "::", "*"):
            a = "127.0.0.1"
        if (a, p) not in out:
            out.append((a, p))
    if ("127.0.0.1", DEFAULT_PORT) not in out:
        out.append(("127.0.0.1", DEFAULT_PORT))
    return out


def _cache_fresh(now):
    if not _TARGET["host"] or not _TARGET["ts"]:
        return False
    ttl = _TARGET_TTL_OK if _TARGET["name"] else _TARGET_TTL_FAIL
    return (now - _TARGET["ts"]) < ttl


def discover(cfg=None, force=False):
    """找到可用的 upsd 与实例名，返回 (host, port, name)。失败抛 RuntimeError。"""
    c = cfg or {}
    now = time.time()
    want = (c.get("name") or NUT_NAME or "").strip()
    key = "%s|%s|%s" % (c.get("host") or "", c.get("port") or "", want)
    if not force and _TARGET["key"] == key and _cache_fresh(now):
        if _TARGET["name"]:
            return _TARGET["host"], _TARGET["port"], _TARGET["name"]
        raise RuntimeError(_TARGET["err"] or "没有找到 UPS")

    last_err = "没有找到 UPS"
    for host, port in candidates(c):
        try:
            ups_list = list_ups(host, port)
        except Exception as e:
            last_err = "连不上 %s:%d（%s）" % (host, port, e)
            continue
        if not ups_list:
            last_err = "%s:%d 上没有配置 UPS 实例" % (host, port)
            continue
        name = want or ups_list[0][0]
        if want:
            names = [n for n, _d in ups_list]
            if want not in names:
                last_err = "实例名 %s 不存在（可用：%s）" % (want, ", ".join(names) or "无")
                continue
        _TARGET.update({"key": key, "host": host, "port": port, "name": name,
                        "ts": now, "err": ""})
        return host, port, name

    _TARGET.update({"key": key, "host": "", "port": 0, "name": "", "ts": now, "err": last_err})
    raise RuntimeError(last_err)


def invalidate():
    _TARGET["ts"] = 0.0


# ---------------------------------------------------------------------------
# 功率解析
# ---------------------------------------------------------------------------
def _num(v):
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x < 0:
        return None
    return x


def _first(v, keys):
    for k in keys:
        x = _num(v.get(k))
        if x is not None:
            return k, x
    return None, None


def power_from_vars(v):
    """从变量表里挑出整机功率（W）。

    返回 {"w": 瓦, "from": 变量名, "load": 负载百分比, "status": 状态串,
          "model": 型号, "battery": 电量百分比}
    取不到功率时 w 为 None。
    """
    out = {"w": None, "from": "", "load": None, "status": "", "model": "", "battery": None}

    if v.get("ups.status"):
        out["status"] = v["ups.status"].strip()
    for k in ("device.model", "ups.model", "ups.productid", "device.mfr"):
        if v.get(k):
            out["model"] = v[k].strip()
            break
    load = _num(v.get("ups.load")) or _num(v.get("output.L1.power.percent"))
    if load is not None and 0 <= load <= 200:
        out["load"] = round(load, 1)
    batt = _num(v.get("battery.charge"))
    if batt is not None and 0 <= batt <= 100:
        out["battery"] = round(batt, 0)

    # 1. 设备直报的有功功率
    key, w = _first(v, ["ups.realpower", "output.power", "output.L1.power",
                        "output.L2.power", "output.L3.power"])
    # 2. ups.power（多数是 W；部分机型给 VA，但仍比估算准）
    if w is None:
        key, w = _first(v, ["ups.power"])
    # 3. 电压 × 电流（直流输出机型只有这一条）
    if w is None:
        for vk, ck in (("output.L1.voltage", "output.L1.current"),
                       ("output.voltage", "output.current"),
                       ("input.L1.voltage", "input.L1.current")):
            volt, cur = _num(v.get(vk)), _num(v.get(ck))
            if volt and cur and volt > 1 and cur > 0:
                key, w = "%s×%s" % (vk, ck), volt * cur
                break
    # 4. 负载百分比 × 额定容量
    if w is None and out["load"] is not None:
        nk, nom = _first(v, ["ups.power.nominal", "ups.realpower.nominal"])
        if nom and nom > 0:
            key, w = "ups.load×%s" % nk, out["load"] / 100.0 * nom

    if w is not None and 0 < w <= MAX_WATT:
        out["w"] = round(w, 1)
        out["from"] = key or ""
    return out


def read(cfg=None):
    """读一次 UPS：返回 {"ok", "host", "port", "name", "error", "vars", "power": {...}}。

    采样循环与页面「检测」按钮共用这一个入口，两边看到的结果不会出现口径差异。
    """
    c = cfg or {}
    try:
        host, port, name = discover(c)
    except Exception as e:
        return {"ok": False, "error": str(e), "power": {}}
    try:
        v = read_vars(host, port, name)
    except Exception as e:
        invalidate()
        return {"ok": False, "error": "读变量失败：%s" % e,
                "host": host, "port": port, "name": name, "power": {}}
    return {"ok": True, "host": host, "port": port, "name": name, "error": "",
            "vars": v, "power": power_from_vars(v)}


def read_power(cfg=None):
    """采样循环用：只关心瓦数。返回 (w, info)，读不到时 w 为 None。"""
    r = read(cfg)
    if not r.get("ok"):
        return None, {"error": r.get("error") or ""}
    p = r.get("power") or {}
    w = p.get("w")
    info = {"w": w, "from": p.get("from") or "", "load": p.get("load"),
            "status": p.get("status") or "", "model": p.get("model") or "",
            "name": r.get("name") or "", "host": r.get("host") or "",
            "port": r.get("port") or 0}
    return w, info


def summary(cfg=None):
    """给页面「检测」用的摘要，只挑人看得懂的几项。"""
    r = read(cfg)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "读取失败", "power": {}}
    v = r.get("vars") or {}
    p = r.get("power") or {}
    keep = {}
    for k in ("ups.status", "ups.load", "ups.power", "ups.realpower", "ups.power.nominal",
              "battery.charge", "battery.runtime", "input.L1-N.voltage", "input.L1.voltage",
              "input.voltage", "output.L1.voltage", "output.voltage",
              "output.L1.current", "output.current", "device.model", "ups.model"):
        if v.get(k):
            keep[k] = v[k]
    return {
        "ok": True,
        "host": r.get("host"), "port": r.get("port"), "name": r.get("name"),
        "power": p, "vars": keep,
    }
