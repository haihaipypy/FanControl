#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消息推送 —— 把 FanControl 的统计与告警推到 AutoDocker 的 /api/push。

三条设计原则：

1. **只做客户端，不实现渠道。** 飞书 / Telegram / 钉钉 / 企业微信的凭据全在
   AutoDocker 那边，这边只存一个令牌。令牌泄了单独撤销即可，不会连管理员
   口令一起漏。
2. **渠道要按单值拆。** AutoDocker 的 `channel` 只接受单个值（all / feishu /
   telegram / dingtalk / wecom），多选就得拆成多次请求再合并结果，不能指望
   传个数组过去。
3. **定时点用「日期 + 时间」做去重键并落盘。** 进程重启、应用升级都不会把
   当天已经发过的点再补一遍 —— 重复推送比漏发更让人烦。

零第三方依赖，只用标准库。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

PKGVAR = os.environ.get("FAN_PKGVAR", "/var/apps/FanControl/var")
STATE_FILE = os.path.join(PKGVAR, "notify_state.json")

CHANNELS = ("feishu", "telegram", "dingtalk", "wecom")
CHANNEL_TEXT = {"all": "全部渠道", "feishu": "飞书", "telegram": "Telegram",
                "dingtalk": "钉钉", "wecom": "企业微信"}
TITLE_MAX = 200          # AutoDocker 侧按字符数限制
BODY_MAX = 8192          # 按字节数限制
MAX_SLOTS = 12
SLOT_KEEP_DAYS = 3
DEFAULT_TIMEOUT = 10

# 守护进程刚起来的这一小段时间不判阈值：此时 state.json 里可能还留着上次
# 运行时的转速，容易一启动就误报。
WARMUP_SECONDS = 90
_STARTED = time.time()

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def norm_channels(raw):
    """规整渠道列表：只留认识的、去重、保持固定顺序。

    返回空列表表示「不指定」，调用方会发 all 交给 AutoDocker 自己挑已就绪的。
    """
    if isinstance(raw, str):
        raw = [x for x in raw.split(",") if x.strip()]
    if not isinstance(raw, (list, tuple)):
        return []
    got = set()
    for x in raw:
        s = str(x or "").strip().lower()
        if s in CHANNELS:
            got.add(s)
    return [c for c in CHANNELS if c in got]


def norm_schedule(raw):
    """定时点规整：时间必须合法，三项内容至少选一个，同一时间只留一条。"""
    out, seen = [], set()
    if not isinstance(raw, (list, tuple)):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        t = str(it.get("time") or "").strip()
        if not _TIME_RE.match(t) or t in seen:
            continue
        item = {"time": t,
                "power": bool(it.get("power")),
                "traffic": bool(it.get("traffic")),
                "plan": bool(it.get("plan"))}
        if not (item["power"] or item["traffic"] or item["plan"]):
            continue
        seen.add(t)
        out.append(item)
        if len(out) >= MAX_SLOTS:
            break
    out.sort(key=lambda x: x["time"])
    return out


def norm_threshold(raw):
    if not isinstance(raw, dict):
        raw = {}
    try:
        speed = int(float(raw.get("speed")))
    except (TypeError, ValueError):
        speed = 80
    speed = max(1, min(100, speed))
    try:
        cool = int(float(raw.get("cooldown_min")))
    except (TypeError, ValueError):
        cool = 30
    cool = max(1, min(1440, cool))
    return {"enabled": bool(raw.get("enabled")), "speed": speed, "cooldown_min": cool}


def norm_base(base):
    """把用户填的地址规整成「到主机+端口为止」。

    允许他直接粘 `http://x:8080/api/push` —— 很多人会这么干，
    多出来的路径要剥掉，否则拼出 /api/push/api/push。
    """
    base = str(base or "").strip()
    if not re.match(r"^https?://", base, re.I):
        return ""
    base = base.rstrip("/")
    if base.lower().endswith("/api/push"):
        base = base[: -len("/api/push")].rstrip("/")
    return base


def conf(cfg):
    n = (cfg or {}).get("notify") or {}
    return {
        "enabled": bool(n.get("enabled")),
        "base_url": norm_base(n.get("base_url")),
        "token": str(n.get("token") or "").strip()[:200],
        "channels": norm_channels(n.get("channels")),
        "schedule": norm_schedule(n.get("schedule")),
        "threshold": norm_threshold(n.get("threshold")),
        "timeout": DEFAULT_TIMEOUT,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _clip_bytes(s, limit):
    """按字节截断且不切坏 UTF-8 —— 中文一个字三字节，直接切会出乱码。"""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", "ignore") + "…"


def _one(c, title, body, channel):
    """发一次请求到指定渠道，返回 {ok, sent, error}。"""
    url = c["base_url"] + "/api/push"
    payload = json.dumps({"title": title, "body": body, "channel": channel},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    # 两个头都带：AutoDocker 都认，但不同语言/库默认只给其中一个
    req.add_header("X-Push-Token", c["token"])
    req.add_header("Authorization", "Bearer " + c["token"])
    try:
        with urllib.request.urlopen(req, timeout=c["timeout"]) as r:
            raw = r.read()
            data = {}
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    data = {}
            return {"ok": bool(data.get("ok")), "sent": data.get("sent") or [],
                    "error": str(data.get("error") or "")}
    except urllib.error.HTTPError as e:
        # 401 令牌无效 / 403 未启用 / 400 参数错 / 502 渠道全挂 —— 都要把
        # 服务端给的原文带出来，否则用户只知道「失败了」，不知道改哪儿。
        msg, sent = "", []
        try:
            raw = e.read()
            data = json.loads(raw.decode("utf-8", "replace"))
            msg = str(data.get("error") or "")
            sent = data.get("sent") or []
        except Exception:
            pass
        if not msg:
            msg = {401: "令牌无效", 403: "推送接口未启用（请先在 AutoDocker 设置页生成令牌）",
                   404: "地址不对：找不到 /api/push"}.get(e.code, "HTTP %d" % e.code)
        return {"ok": False, "sent": sent, "error": msg}
    except urllib.error.URLError as e:
        return {"ok": False, "sent": [], "error": "连不上：%s" % (getattr(e, "reason", e),)}
    except OSError as e:
        # 连接被重置（端口没开、中间设备拦截）时 urlopen 有时直接抛裸 OSError，
        # 不会包成 URLError —— 不单独接住的话错误文本会只剩一句 WinError。
        return {"ok": False, "sent": [], "error": "连不上：%s" % e}
    except Exception as e:
        return {"ok": False, "sent": [], "error": str(e)}


def push(c, title, body, channels=None):
    """推一条消息。返回 {ok, sent, error}，ok 表示至少一个渠道发出去了。"""
    if not c.get("base_url"):
        return {"ok": False, "sent": [], "error": "未填写推送地址"}
    if not c.get("token"):
        return {"ok": False, "sent": [], "error": "未填写推送令牌"}

    title = (title or "").strip()[:TITLE_MAX]
    body = _clip_bytes((body or "").strip(), BODY_MAX)
    if not title and not body:
        return {"ok": False, "sent": [], "error": "标题与正文都为空"}

    chans = norm_channels(channels if channels is not None else c.get("channels"))
    # 没选 或 四个全选 → 发 all，交给 AutoDocker 挑已就绪的渠道。
    # 中间档（1~3 个）AutoDocker 不支持，只能拆请求。
    targets = ["all"] if (not chans or len(chans) == len(CHANNELS)) else chans

    sent, errors, any_ok = [], [], False
    for ch in targets:
        r = _one(c, title, body, ch)
        if r.get("ok"):
            any_ok = True
        sent.extend(r.get("sent") or [])
        if not r.get("ok"):
            errors.append("%s：%s" % (CHANNEL_TEXT.get(ch, ch), r.get("error") or "发送失败"))
    return {"ok": any_ok, "sent": sent, "error": "" if any_ok else "；".join(errors)}


# ---------------------------------------------------------------------------
# 内容组装
#
# 四个渠道的渲染能力各不相同，这里只能取交集：飞书是纯 text，Telegram 走
# HTML parse_mode 但正文会被整体转义（写 <b> 会原样露出来），钉钉是卡片参数，
# 企微是 markdown（不认 emoji 以外的复杂语法）。所以排版只用「emoji + 换行 +
# 全角缩进 + 分隔线」这类纯字符手段，不用 * 加粗、# 标题、- 列表 —— 那些在
# 至少一个渠道上会露出原文。
# ---------------------------------------------------------------------------
SEP = "────────────────"      # 16 个 U+2500，约等于手机气泡的一整行宽
IND = "\u3000\u3000"          # 块内数据行的缩进（全角双空格）
GAP = "\u3000"                # 标签与数值之间（全角单空格）


def _block(title, rows):
    """一个区块：标题行 + 缩进后的数据行。"""
    return [title] + [IND + r for r in rows]


def _fmt_kwh(v):
    return "%.3f" % v if abs(v) < 1 else "%.2f" % v


def _fmt_bytes(n):
    """按量级收小数位。

    手机气泡一行只放得下二十来个半角宽，「839.44 MB」这种六位数字加上标签和
    缩进就会折行、把 MB 甩到下一行 —— 这是之前排版最难看的地方，所以越大的
    量级小数位越少：三位数取整，两位数留一位，个位数才留两位。
    """
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0 or n >= 100:
        s = "%.0f" % n
    elif n >= 10:
        s = "%.1f" % n
    else:
        s = "%.2f" % n
    # 999.94 MB 收成整数会变成「1000 MB」，看着别扭，哪怕还没到 1024 也进位
    if s == "1000" and i < len(units) - 1:
        i += 1
        s = "1.00"
    return s + " " + units[i]


def _fmt_ts(ts):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


def _yesterday_power(cfg, now=None):
    """昨天（自然日）的耗电与电费。返回 dict，缺数据时 ok=False，不编数。"""
    try:
        import energy
        s = energy.summary(cfg, 3)
        days = s.get("days") or []
        rec = days[-2] if len(days) >= 2 else None
    except Exception as e:
        return {"ok": False, "rows": ["读取失败（%s）" % e]}
    if not rec or rec.get("kwh") is None:
        return {"ok": False, "rows": ["昨天没有数据"]}
    rows = ["电量" + GAP + _fmt_kwh(float(rec.get("kwh") or 0)) + " kWh",
            "电费" + GAP + "¥%.2f" % float(rec.get("cost") or 0)]
    if rec.get("avg_w") is not None:
        rows.append("平均" + GAP + "%s W" % rec["avg_w"])
    return {"ok": True, "rows": rows}


def _yesterday_traffic(cfg):
    try:
        import netflow
        s = netflow.summary(cfg, 3)
        days = s.get("days") or []
        rec = days[-2] if len(days) >= 2 else None
    except Exception as e:
        return {"ok": False, "rows": ["读取失败（%s）" % e]}
    if not rec or rec.get("rx") is None:
        return {"ok": False, "rows": ["昨天没有数据"]}
    rx, tx = float(rec.get("rx") or 0), float(rec.get("tx") or 0)
    # 上下行拆两行。写在一行「↓ 839.44 MB　↑ 102.48 MB」会超宽，飞书直接把
    # 后一个 MB 折到下一行，看着像排版断了。
    return {"ok": True,
            "rows": ["下行" + GAP + _fmt_bytes(rx),
                     "上行" + GAP + _fmt_bytes(tx),
                     "合计" + GAP + _fmt_bytes(rx + tx)]}


def _plan_info():
    try:
        import powersched
        st = powersched.status()
    except Exception as e:
        return {"ok": False, "rows": ["读取失败（%s）" % e]}
    on, off = st.get("next_on"), st.get("next_off")
    if not on and not off:
        enabled = bool((st.get("plan") or {}).get("enabled"))
        return {"ok": False,
                "rows": ["没有启用的开关机计划" if enabled else "定时开关机总开关已关闭"]}
    rows = []
    if on:
        rows.append("开机" + GAP + _fmt_ts(on))
    if off:
        rows.append("关机" + GAP + _fmt_ts(off))
    return {"ok": True, "rows": rows}


def build_daily(cfg, item, now=None):
    """拼一条「日报」。只包含这一项勾选的内容，缺数据的项如实标注。

    排版：每个项目单独成块，块尾横贯一条分隔线，块内数据行统一缩进、标签
    固定两个字（电量/电费/平均、下行/上行/合计、开机/关机）。这样竖着看是
    一列对齐的数字，横着看不超宽 —— 手机气泡里既不会折行，也能一眼分出
    哪几行属于同一个项目。
    """
    now = now or time.time()
    ymd = time.strftime("%m-%d", time.localtime(now - 86400))

    blocks = []
    if item.get("power"):
        blocks.append(_block("⚡ 功耗", _yesterday_power(cfg, now)["rows"]))
    if item.get("traffic"):
        blocks.append(_block("🌐 流量", _yesterday_traffic(cfg)["rows"]))
    if item.get("plan"):
        blocks.append(_block("⏰ 开关机计划", _plan_info()["rows"]))

    lines = []
    for b in blocks:
        lines.extend(b)
        lines.append(SEP)     # 每块尾部封线，页脚就被自然隔开了
    lines.append("🕘 " + time.strftime("%H:%M", time.localtime(now)) + " 自动汇总")

    title = "📊 FanControl 日报 · " + ymd
    return title, "\n".join(lines)


def build_threshold(fan_state, speed, limit, now=None):
    now = now or time.time()
    rows = []
    if fan_state.get("cpu_temp") is not None:
        rows.append("核心" + GAP + "%s°C" % fan_state["cpu_temp"])
    if fan_state.get("hdd_temp") is not None:
        rows.append("硬盘" + GAP + "%s°C" % fan_state["hdd_temp"])
    if not rows:
        rows.append("温度读不到")
    # 转速与阈值已经在标题里说过了，正文只补温度，不重复
    lines = _block("🌡 当前温度", rows) + [
        SEP, "🕘 " + time.strftime("%m-%d %H:%M", time.localtime(now))]
    title = "🔥 风扇转速 %d%%，已超过阈值 %d%%" % (speed, limit)
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# 状态（去重 + 冷却）
# ---------------------------------------------------------------------------
def load_state():
    data = {}
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    slots = data.get("slots")
    if not isinstance(slots, dict):
        slots = {}
    clean = {}
    for k, v in slots.items():
        try:
            clean[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    try:
        thr = float(data.get("threshold_ts") or 0)
    except (TypeError, ValueError):
        thr = 0.0
    last = data.get("last")
    return {"v": 1, "slots": clean, "threshold_ts": thr,
            "last": last if isinstance(last, dict) else {}}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, STATE_FILE)
        return True
    except Exception:
        return False


def _prune(st, now=None):
    """去重键只留最近几天，别让文件无限长。"""
    now = now or time.time()
    cutoff = time.strftime("%Y-%m-%d", time.localtime(now - SLOT_KEEP_DAYS * 86400))
    slots = st.get("slots") or {}
    for k in list(slots.keys()):
        if k[:10] < cutoff:
            del slots[k]
    return st


# ---------------------------------------------------------------------------
# 主循环调用
# ---------------------------------------------------------------------------
def tick(cfg, fan_state=None, now=None):
    """跑一轮检查：到点的定时推送 + 阈值推送。返回本轮真正发出去的记录。"""
    c = conf(cfg)
    if not c["enabled"]:
        return []

    now = now or time.time()
    st = load_state()
    dirty = False
    fired = []

    hhmm = time.strftime("%H:%M", time.localtime(now))
    day = time.strftime("%Y-%m-%d", time.localtime(now))

    for item in c["schedule"]:
        if item["time"] != hhmm:
            continue
        key = day + " " + hhmm
        if key in st["slots"]:
            continue
        title, body = build_daily(cfg, item, now)
        r = push(c, title, body)
        st["slots"][key] = now
        st["last"] = {"ts": now, "kind": "schedule", "title": title,
                      "ok": r["ok"], "error": r["error"], "sent": r["sent"]}
        dirty = True
        fired.append({"kind": "schedule", "time": hhmm, "title": title,
                      "ok": r["ok"], "error": r["error"], "sent": r["sent"]})

    t = c["threshold"]
    # 开机全速期间不判阈值：那一段本来就是故意拉满的，报了也是噪音。
    if (t["enabled"] and fan_state is not None
            and now - _STARTED > WARMUP_SECONDS):
        speed = fan_state.get("current_speed")
        if speed is None:
            speed = fan_state.get("target_speed")
        try:
            speed = int(speed)
        except (TypeError, ValueError):
            speed = None
        if (speed is not None and speed >= t["speed"]
                and not fan_state.get("boot_boost_active")
                and not fan_state.get("paused")):
            if now - float(st.get("threshold_ts") or 0) >= t["cooldown_min"] * 60:
                title, body = build_threshold(fan_state, speed, t["speed"], now)
                r = push(c, title, body)
                st["threshold_ts"] = now
                st["last"] = {"ts": now, "kind": "threshold", "title": title,
                              "ok": r["ok"], "error": r["error"], "sent": r["sent"]}
                dirty = True
                fired.append({"kind": "threshold", "speed": speed, "title": title,
                              "ok": r["ok"], "error": r["error"], "sent": r["sent"]})

    if dirty:
        _prune(st, now)
        save_state(st)
    return fired


def test(cfg, overrides=None):
    """页面上的「发一条测试」。overrides 让用户填完还没保存就能先验。"""
    c = conf(cfg)
    o = overrides or {}
    if o.get("base_url") is not None:
        c["base_url"] = norm_base(o.get("base_url"))
    if o.get("token") is not None:
        c["token"] = str(o.get("token") or "").strip()[:200]
    if o.get("channels") is not None:
        c["channels"] = norm_channels(o.get("channels"))
    body = ("✅ 地址、令牌、渠道都通了。\n"
            + SEP + "\n"
            + "🕘 " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            + "\n这条消息由风扇温控的「发送测试」发出。")
    return push(c, "✅ FanControl 连接测试", body)


def last_result():
    st = load_state()
    return {"last": st.get("last") or {}, "threshold_ts": st.get("threshold_ts") or 0,
            "slots": sorted((st.get("slots") or {}).keys())[-5:]}
