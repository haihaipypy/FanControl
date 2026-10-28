#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时开关机调度 —— 飞牛 fnOS 应用 FanControl

职责：
  1. 定时开机：把下一次开机时间写进主板 RTC 唤醒闹钟（/sys/class/rtc/rtcN/wakealarm）
  2. 定时关机 / 重启：按分钟粒度检查计划，到点执行 /sbin/poweroff、/sbin/reboot
  3. 每次巡检把 RTC 闹钟与计划对齐，写状态文件供 Web 页面显示

由 fand.py 以**后台线程**方式启动（15 秒一轮），不单独占一个进程 ——
这样跟着应用的 systemd 自启服务走，无人值守开机后调度器一定在跑。

⚠️ 三个必须知道的坑（实测总结，改代码前先看）：
  1) 内核把 wakealarm 里写的 epoch **按 UTC 解释**后直接塞进 RTC 寄存器。
     如果主板时钟走本地时间（BIOS 里存的是 CST 而非 UTC），
     直接传本地时间戳会早/晚一个时区差（北京 8 小时）才叫醒。
     做法：拿 /sys/class/rtc/rtc0/since_epoch 和系统时间比出偏差，写的时候补回去。
  2) 闹钟必须**每次巡检都对齐**，不能「设一次管一辈子」：
     计划改了、系统重启、掉电都会让寄存器停在旧值。
  3) 同一分钟只执行一次。轮询粒度 15 秒，不记 key 会在一分钟内重复触发关机。

零第三方依赖，仅 Python 标准库。
"""

import json
import os
import random
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, time as dtime

# ---------------------------------------------------------------------------
# 路径（与 fand.py 保持一致，可由环境变量覆盖，便于测试）
# ---------------------------------------------------------------------------
PKGECT = os.environ.get("FAN_PKGETC", "/var/apps/FanControl/etc")
PKGVAR = os.environ.get("FAN_PKGVAR", "/var/apps/FanControl/var")

PLAN_FILE = os.path.join(PKGECT, "power.json")
PSTATE_FILE = os.path.join(PKGVAR, "power_state.json")
LOG_FILE = os.path.join(PKGVAR, "fan_control.log")

# sysfs RTC 根目录。抽成常量，本地测试时可以注入假目录。
RTC_BASE = "/sys/class/rtc"

# 执行器轮询粒度（秒）。必须明显小于 60，否则会漏掉整分钟。
TICK_SECONDS = 15

# RTC 偏差取整粒度：900 秒 = 15 分钟，能容纳 UTC+5:30 / +5:45 这类半小时时区
OFFSET_QUANTUM = 900

# 允许的偏差上限，超过认为是读到了不可信的 RTC 值
OFFSET_SANE_LIMIT = 86400

# 闹钟已对齐的容差（秒）。差值小于它就认为不用重写，避免每 15 秒都去写 sysfs。
ALARM_TOLERANCE = 60

ACTIONS = ("on", "off", "reboot")
ACTION_LABEL = {"on": "开机", "off": "关机", "reboot": "重启"}
WEEK_NAMES = ("一", "二", "三", "四", "五", "六", "日")

DEFAULT_PLAN = {
    "enabled": True,      # 总开关
    "items": [],          # 计划条目
}


# ---------------------------------------------------------------------------
# 日志（与 fand.py 写同一个文件，前缀区分来源）
# ---------------------------------------------------------------------------
def log(msg):
    line = "[%s] [power] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 计划文件
# ---------------------------------------------------------------------------
def _new_id():
    return "%08x" % random.getrandbits(32)


def _norm_item(raw):
    """把一条计划规整成合法结构，非法返回 None"""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return None
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", str(raw.get("time") or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    days = []
    for d in (raw.get("days") or []):
        try:
            d = int(d)
        except (TypeError, ValueError):
            continue
        if 0 <= d <= 6 and d not in days:
            days.append(d)
    days.sort()
    return {
        "id": str(raw.get("id") or "").strip() or _new_id(),
        "action": action,
        "time": "%02d:%02d" % (hh, mm),
        "days": days,                       # 空列表 = 每天
        "enabled": bool(raw.get("enabled", True)),
        "note": str(raw.get("note") or "")[:40],
    }


def normalize_plan(raw):
    plan = {"enabled": bool((raw or {}).get("enabled", True)), "items": []}
    for it in ((raw or {}).get("items") or []):
        n = _norm_item(it)
        if n:
            plan["items"].append(n)
    plan["items"].sort(key=lambda x: (x["time"], ACTION_LABEL.get(x["action"], "")))
    return plan


def load_plan():
    if os.path.isfile(PLAN_FILE):
        try:
            with open(PLAN_FILE, "r", encoding="utf-8") as f:
                return normalize_plan(json.load(f))
        except Exception as e:
            log("计划文件解析失败，按空计划处理: %s" % e)
    return normalize_plan(DEFAULT_PLAN)


def save_plan(plan):
    plan = normalize_plan(plan)
    os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
    tmp = PLAN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PLAN_FILE)
    return plan


# ---------------------------------------------------------------------------
# 调度状态（与温控的 state.json 分开存，互不干扰）
# ---------------------------------------------------------------------------
def read_pstate():
    if os.path.isfile(PSTATE_FILE):
        try:
            with open(PSTATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def write_pstate(**kwargs):
    st = read_pstate()
    st.update(kwargs)
    st["updated_at"] = time.time()
    try:
        os.makedirs(os.path.dirname(PSTATE_FILE), exist_ok=True)
        tmp = PSTATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PSTATE_FILE)
    except Exception as e:
        log("写调度状态失败: %s" % e)


def _push_event(action, extra=""):
    """往状态文件里追加一条执行记录（最多保留 10 条）"""
    st = read_pstate()
    events = st.get("events") or []
    events.insert(0, {
        "ts": time.time(),
        "action": action,
        "label": ACTION_LABEL.get(action, action),
        "text": extra,
    })
    write_pstate(events=events[:10])


# ---------------------------------------------------------------------------
# RTC 唤醒闹钟
# ---------------------------------------------------------------------------
def find_rtc():
    """返回 (rtc 目录, wakealarm 路径)；没有可用 RTC 时返回 (None, None)"""
    try:
        names = sorted(os.listdir(RTC_BASE))
    except Exception:
        return None, None
    for n in names:
        if not n.startswith("rtc"):
            continue
        d = os.path.join(RTC_BASE, n)
        p = os.path.join(d, "wakealarm")
        if os.path.exists(p):
            return d, p
    return None, None


def rtc_read_alarm():
    """读取当前闹钟（本地时间戳）。未设置返回 None"""
    _, p = find_rtc()
    if not p:
        return None
    try:
        with open(p, "r") as f:
            raw = f.read().strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except (TypeError, ValueError):
        return None


def rtc_offset():
    """主板时钟与系统 UTC 的偏差（秒）

    主板走本地时间时是 3600 的整数倍（北京 = +28800）；
    走 UTC 时约等于 0（仅剩晶振漂移）。取整到 15 分钟，避免漂移让值天天变。
    """
    d, _ = find_rtc()
    if not d:
        return 0
    try:
        with open(os.path.join(d, "since_epoch"), "r") as f:
            rtc_epoch = int(f.read().strip())
    except Exception:
        return 0
    off = rtc_epoch - int(time.time())
    if abs(off) > OFFSET_SANE_LIMIT:
        return 0
    return int(round(off / float(OFFSET_QUANTUM)) * OFFSET_QUANTUM)


def offset_text(off):
    if not off:
        return "与系统时钟一致"
    sign = "+" if off > 0 else "-"
    off = abs(int(off))
    h, m = off // 3600, (off % 3600) // 60
    return "主板时钟快 %s%d 小时 %d 分（按本地时间走）" % (sign, h, m) if off > 0 \
        else "主板时钟慢 %d 小时 %d 分" % (h, m)


def rtc_write_alarm(target_epoch, offset=None):
    """写入唤醒时间（参数是**本地时间戳**）。返回 (ok, err, 实际写入值)"""
    _, p = find_rtc()
    if not p:
        return False, "未找到 RTC 唤醒接口（/sys/class/rtc/*/wakealarm）", None
    if offset is None:
        offset = rtc_offset()
    value = int(target_epoch + offset)
    try:
        # 先清零：部分驱动在已有闹钟时直接覆盖会返回 EBUSY
        with open(p, "w") as f:
            f.write("0")
        with open(p, "w") as f:
            f.write(str(value))
    except Exception as e:
        return False, "写 RTC 闹钟失败: %s" % e, None
    return True, None, value


def rtc_clear_alarm():
    _, p = find_rtc()
    if not p:
        return False
    try:
        with open(p, "w") as f:
            f.write("0")
        return True
    except Exception:
        return False


def rtc_info():
    """给页面用的 RTC 快照"""
    d, p = find_rtc()
    alarm = rtc_read_alarm()
    off = rtc_offset() if d else 0
    return {
        "supported": bool(p),
        "path": p,
        "alarm": alarm,                       # 真实生效的唤醒时间（本地时间戳）
        "offset": off,
        "offset_text": offset_text(off),
        "error": None if p else "系统未暴露 RTC 唤醒接口",
    }


# ---------------------------------------------------------------------------
# 计划时间计算
# ---------------------------------------------------------------------------
def _item_days(item):
    return item.get("days") or list(range(7))


def next_occurrence(item, now=None):
    """该条目下一次触发时间；9 天内都没有返回 None"""
    now = now or datetime.now()
    try:
        hh, mm = [int(x) for x in item["time"].split(":")]
    except Exception:
        return None
    days = _item_days(item)
    for i in range(0, 9):
        day = (now + timedelta(days=i)).date()
        if day.weekday() not in days:
            continue
        cand = datetime.combine(day, dtime(hh, mm))
        if cand > now:
            return cand
    return None


def preview(plan, horizon=7, now=None):
    """未来 N 天的执行预览，按时间排序"""
    now = now or datetime.now()
    limit = now + timedelta(days=horizon)
    out = []
    for it in plan.get("items") or []:
        if not it.get("enabled"):
            continue
        cursor = now
        for _ in range(0, horizon * 2):
            nxt = next_occurrence(it, cursor)
            if nxt is None or nxt > limit:
                break
            out.append({
                "ts": nxt.timestamp(),
                "action": it["action"],
                "label": ACTION_LABEL.get(it["action"], it["action"]),
                "time": it["time"],
                "week": "周" + WEEK_NAMES[nxt.weekday()],
                "note": it.get("note") or "",
            })
            cursor = nxt
    out.sort(key=lambda x: x["ts"])
    return out


def next_event(plan, actions, now=None):
    now = now or datetime.now()
    best = None
    for it in plan.get("items") or []:
        if not it.get("enabled") or it["action"] not in actions:
            continue
        nxt = next_occurrence(it, now)
        if nxt is None:
            continue
        if best is None or nxt < best:
            best = nxt
    return best


# ---------------------------------------------------------------------------
# RTC 对齐
# ---------------------------------------------------------------------------
def sync_rtc(plan=None, force=False):
    """把「下一次开机」写进主板 RTC。force=True 时无视对齐容差直接重写。

    返回 dict：{ok, action, target, error, cleared}
    """
    plan = plan if plan is not None else load_plan()
    res = {"ok": True, "action": "none", "target": None,
           "error": None, "cleared": False}

    # 总开关关闭 -> 只清掉「我们自己写过的」闹钟，不碰用户手动设的
    if not plan.get("enabled"):
        if read_pstate().get("rtc"):
            if rtc_clear_alarm():
                write_pstate(rtc={})
                res["action"] = "cleared"
                res["cleared"] = True
                log("定时开关机已关闭，清除 RTC 唤醒闹钟")
        return res

    nxt = next_event(plan, ("on",))
    if nxt is None:
        return res          # 没有开机计划：不动 RTC（可能是 BIOS 自带闹钟）

    target = int(nxt.timestamp())
    off = rtc_offset()
    cur = rtc_read_alarm()
    if not force and cur is not None and abs(cur - (target + off)) <= ALARM_TOLERANCE:
        res["target"] = target
        return res          # 已对齐，不重复写 sysfs

    ok, err, written = rtc_write_alarm(target, off)
    if not ok:
        res.update(ok=False, error=err)
        log("RTC 闹钟写入失败: %s" % err)
        return res

    res["target"] = target
    res["action"] = "written"
    write_pstate(rtc={"epoch": written, "target": target, "at": time.time()})
    log("RTC 闹钟已同步 -> %s（主板寄存器 %d，偏差 %+ds）"
        % (nxt.strftime("%m-%d %H:%M"), written, off))
    return res


# ---------------------------------------------------------------------------
# 执行关机 / 重启
# ---------------------------------------------------------------------------
def _power_cmd(action):
    name = "poweroff" if action == "off" else "reboot"
    for d in ("/sbin", "/usr/sbin", "/bin", "/usr/bin"):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return [p]
    w = shutil.which(name)
    return [w] if w else None


def run_power_action(action):
    """执行关机/重启。返回 (ok, err)"""
    cmd = _power_cmd(action)
    if not cmd:
        return False, "未找到 %s 命令" % ("poweroff" if action == "off" else "reboot")
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True, None
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 单次巡检
# ---------------------------------------------------------------------------
def tick(now=None, allow_exec=True):
    """检查计划，到点执行。返回本次触发的条目列表"""
    now = now or datetime.now()
    plan = load_plan()
    pst = read_pstate()
    fired = []

    if plan.get("enabled"):
        minute_key = now.strftime("%Y%m%d%H%M")
        today = now.strftime("%H:%M")
        last_run = pst.get("last_run") or {}

        due = []
        for it in plan.get("items") or []:
            if not it.get("enabled"):
                continue
            if now.weekday() not in _item_days(it):
                continue
            if it["time"] != today:
                continue
            if last_run.get(it["id"]) == minute_key:
                continue          # 这一分钟已经跑过
            due.append(it)

        # 同一分钟既有关机又有重启时，关机优先（避免先重启又关机）
        order = {"off": 0, "reboot": 1, "on": 2}
        due.sort(key=lambda x: order.get(x["action"], 9))

        # 先把这一分钟命中的**全部**标记为已处理，再执行。
        # 只标记被执行的那一个是不行的：15 秒后的下一轮会把剩下那条再执行一遍。
        for it in due:
            last_run[it["id"]] = minute_key
        write_pstate(last_run=last_run)

        for it in due:
            act = it["action"]
            fired.append({"action": act, "time": it["time"]})

            if act == "on":
                # 机器正在运行，开机计划此时只是个标记，不做任何事
                log("定时开机计划命中 %s（机器已在运行，无需操作）" % it["time"])
                _push_event("on", "计划 %s，机器已在运行" % it["time"])
                continue

            if not allow_exec:
                log("定时%s计划命中 %s（演练模式，未执行）"
                    % (ACTION_LABEL.get(act, act), it["time"]))
                continue

            # 关机/重启前先把下一次开机时间写死，防止计划之后被改
            plan_now = load_plan()
            sync_rtc(plan_now, force=True)
            nxt = next_event(plan_now, ("on",))
            if nxt:
                log("定时%s：将在 %s 唤醒" %
                    (ACTION_LABEL.get(act, act), nxt.strftime("%m-%d %H:%M")))
            else:
                log("定时%s：没有配置开机计划，关机后不会被自动唤醒"
                    % ACTION_LABEL.get(act, act))

            ok, err = run_power_action(act)
            if ok:
                log("定时%s已执行（%s）" % (ACTION_LABEL.get(act, act), it["time"]))
                _push_event(act, "计划 %s" % it["time"])
            else:
                log("定时%s执行失败: %s" % (ACTION_LABEL.get(act, act), err))
                _push_event(act, "执行失败: %s" % err)
            break       # 一次巡检只执行一个电源动作

        write_pstate(last_run=last_run)

    write_pstate(last_tick=time.time())
    return fired


def status(plan=None):
    """给 Web 页面用的完整快照"""
    plan = plan if plan is not None else load_plan()
    pst = read_pstate()
    pv = preview(plan, 7)
    nxt_on = next_event(plan, ("on",))
    nxt_off = next_event(plan, ("off", "reboot"))
    return {
        "ok": True,
        "plan": plan,
        "preview": pv,
        "next_on": nxt_on.timestamp() if nxt_on else None,
        "next_off": nxt_off.timestamp() if nxt_off else None,
        "rtc": rtc_info(),
        "scheduler": {
            "alive": (time.time() - float(pst.get("last_tick") or 0)) < 120,
            "last_tick": pst.get("last_tick"),
            "tick_seconds": TICK_SECONDS,
        },
        "events": pst.get("events") or [],
        "now": time.time(),
    }


def describe():
    """启动日志用的一行摘要"""
    plan = load_plan()
    d, _ = find_rtc()
    on_cnt = len([i for i in plan["items"] if i["action"] == "on" and i["enabled"]])
    off_cnt = len([i for i in plan["items"]
                   if i["action"] in ("off", "reboot") and i["enabled"]])
    return "计划 %d 条（开机 %d / 关机重启 %d），总开关 %s，RTC %s" % (
        len(plan["items"]), on_cnt, off_cnt,
        "开" if plan.get("enabled") else "关",
        "可用" if d else "不可用")


# ---------------------------------------------------------------------------
# 命令行入口（便于本地调试：python3 powersched.py --now "2026-10-09 07:30"）
# ---------------------------------------------------------------------------
def _main(argv):
    if "--once" in argv:
        print("计划:", json.dumps(load_plan(), ensure_ascii=False, indent=2))
        print("状态:", json.dumps(status(), ensure_ascii=False, indent=2, default=str))
        return 0
    if "--sync" in argv:
        print(json.dumps(sync_rtc(force=True), ensure_ascii=False, default=str))
        return 0
    if "--dry" in argv:
        fired = tick(allow_exec=False)
        print("模拟触发:", json.dumps(fired, ensure_ascii=False))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
