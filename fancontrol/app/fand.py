#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风扇温控守护进程 — 飞牛 fnOS 应用 FanControl 的核心控制逻辑

职责：
  1. 周期性读取 CPU / 硬盘温度
  2. 按分档曲线计算目标转速
  3. 调用 fanctrl 二进制下发转速（仅变化时下发）
  4. 写状态文件供 Web 后端读取

零第三方依赖，仅使用 Python 标准库。
"""

try:
    import fcntl
except ImportError:      # 非 Linux（开发机测试用），锁功能降级
    fcntl = None
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# 路径常量（可由环境变量覆盖，便于测试）
# ---------------------------------------------------------------------------
APPDIR = os.environ.get("FAN_APPDIR", "/var/apps/FanControl/target")
PKGECT = os.environ.get("FAN_PKGETC", "/var/apps/FanControl/etc")
PKGVAR = os.environ.get("FAN_PKGVAR", "/var/apps/FanControl/var")

FANCTRL = os.environ.get("FAN_CTRL_PATH", os.path.join(APPDIR, "bin", "fanctrl"))
# fanctrl 优先用应用自带的；老用户机器上 /root/fanctrl 已存在时作为兜底
FANCTRL_FALLBACK = "/root/fanctrl"

CONFIG_FILE = os.path.join(PKGECT, "config.json")
STATE_FILE = os.path.join(PKGVAR, "state.json")
LOG_FILE = os.path.join(PKGVAR, "fan_control.log")
LOCK_FILE = os.path.join(PKGVAR, "fand.lock")

# sysfs 传感器根目录。抽成常量便于本地测试时注入假目录。
HWMON_BASE = "/sys/class/hwmon"
THERMAL_BASE = "/sys/class/thermal"

# 迟滞 / 渐变 / 紧急直通 —— 三个参数用于消除「边界抖动」和「瞬时尖峰猛冲」
#   hysteresis    : 迟滞温度（℃）。升档需超过当前档上界 + 该值，降档需低于上一档上界 - 该值。
#                   没有它时，空闲温度骑在分界线上会每 30 秒翻一次档（实测日志出现过 15 次/2.5h）。
#   slew_step     : 单次巡检最多变化几档。1 = 逐级走（35→50→70→100），杜绝 35% 直冲 100%。
#   emergency_cpu : CPU 超过该温度时无视迟滞与渐变，直接满速。
#   emergency_hdd : 硬盘超过该温度时同理。
HYSTERESIS_DEFAULT = 3
SLEW_STEP_DEFAULT = 1
EMERGENCY_CPU_DEFAULT = 90
EMERGENCY_HDD_DEFAULT = 70

# ---------------------------------------------------------------------------
# 默认配置 —— 分档曲线
# 每档: { "cpu": CPU上限, "hdd": 硬盘上限, "speed": 转速% }
# 计算方式：CPU 与硬盘各自从第一档往下找到自己命中的档（`温度 <= 阈值` 即命中），
#           两个结果取"较高转速"（谁热听谁的）。
# 阈值为「该档温度上界」，前档优先。
#
# 默认值按 Intel N150（Tjmax 105℃） + NAS 机械盘（寿命线 40℃，50℃ 起故障率上行）
# 标定，目标：空闲 35% 静音，且不出现边界抖动。
#   CPU  侧实际生效区间： ≤45→25% / 46~60→35% / 61~68→50% / 69~78→70% / ≥79→100%
#   硬盘 侧实际生效区间： ≤45→25% / 46~55→35% / 56~60→50% / 61~66→70% / ≥67→100%
# 叠加迟滞后，35% 档实际会一直保持到 CPU 63℃ / 硬盘 58℃ 才升档。
# CPU 温度余量很大，阈值给得宽；硬盘是寿命短板，61℃ 起就压到 100%。
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "enabled": True,             # 温控守护总开关
    "check_interval": 30,        # 检测间隔（秒）
    "min_speed": 20,             # 最低转速下限（%）
    "max_speed": 100,            # 最高转速上限（%）
    "curves": [
        {"cpu": 45, "hdd": 45, "speed": 25},
        {"cpu": 60, "hdd": 55, "speed": 35},
        {"cpu": 68, "hdd": 60, "speed": 50},
        {"cpu": 78, "hdd": 66, "speed": 70},
        {"cpu": 999, "hdd": 999, "speed": 100},
    ],
    "hysteresis": HYSTERESIS_DEFAULT,      # 迟滞温度（℃），0 = 关闭
    "slew_step": SLEW_STEP_DEFAULT,        # 单次最多变化档数，0 = 不限制
    "emergency_cpu": EMERGENCY_CPU_DEFAULT,
    "emergency_hdd": EMERGENCY_HDD_DEFAULT,
    "hdd_devices_blacklist": [],  # 排除的块设备名，如 ["sda"]
}


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as e:
            log("配置文件解析失败，使用默认配置: %s" % e)
    # 规范化
    cfg["curves"] = sorted(cfg.get("curves") or DEFAULT_CONFIG["curves"],
                           key=lambda c: (c.get("cpu", 999), c.get("hdd", 999)))
    return cfg


def save_default_config_if_missing() -> None:
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        if not os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            log("已生成默认配置文件: %s" % CONFIG_FILE)
    except Exception as e:
        log("写入默认配置失败: %s" % e)


# ---------------------------------------------------------------------------
# 状态读写
# ---------------------------------------------------------------------------
def read_state() -> dict:
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def write_state(**kwargs) -> None:
    st = read_state()
    st.update(kwargs)
    st["updated_at"] = time.time()
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("写状态文件失败: %s" % e)


# ---------------------------------------------------------------------------
# 温度读取
#
# ⚠️ 传感器选择是这套方案最容易出错的地方：**读到了值 ≠ 读对了传感器**。
# 实测踩过的坑（拾光坞 G2）：
#   1) /sys/class/thermal/thermal_zone0 是 acpitz（ACPI 热区），
#      常年给一个恒定假值（如永远 27℃），绝不能当 CPU 温度用。
#   2) 按 tempN_input 的**序号**挑传感器不可靠 —— 不同主板/内核下顺序不一样。
#      必须按 tempN_label 匹配（coretemp 的 "Package id 0"、k10temp 的 "Tctl"）。
#   3) 硬盘温度优先走 hwmon 的 drivetemp（内核直读，**不唤醒休眠盘**），
#      smartctl 只作兜底。
# 因此：hwmon 提为首选，acpitz 明确排除，并记录实际取值来源供页面显示。
# ---------------------------------------------------------------------------

# CPU 相关的 hwmon 芯片名（小写匹配）
CPU_HWMON_CHIPS = (
    "coretemp",      # Intel
    "k10temp",       # AMD
    "zenpower",      # AMD（第三方驱动）
    "x86_pkg_temp",  # Intel 包温（部分内核）
    "cpu_thermal",   # ARM SoC
    "cpu-thermal",
)

# CPU label 偏好（按顺序匹配，越靠前越优先）
CPU_LABEL_PREFER = (
    "package id 0",  # Intel 整包温度
    "tctl",          # AMD 控制温度
    "tdie",          # AMD 核心温度
    "package",
    "cpu",
)


def _read_millideg(path):
    """读 sysfs 毫摄氏度文件，返回整数摄氏度；无效返回 None"""
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        if not raw:
            return None
        t = int(raw) // 1000
        if 0 < t < 150:
            return t
    except Exception:
        pass
    return None


def _hwmon_chips():
    """枚举 /sys/class/hwmon，返回 [(目录, 原始chip名, 小写chip名)]"""
    out = []
    base = HWMON_BASE
    try:
        entries = sorted(os.listdir(base))
    except Exception:
        return out
    for name in entries:
        d = os.path.join(base, name)
        try:
            with open(os.path.join(d, "name"), "r") as f:
                chip = f.read().strip()
        except Exception:
            continue
        out.append((d, chip, chip.lower()))
    return out


def _block_name_from_device(devlink):
    """由 hwmon 的 device 软链反查块设备名（sda / nvme0n1 ...）"""
    try:
        real = os.path.realpath(devlink)
    except Exception:
        return None
    if not real or not os.path.exists(real):
        return None
    parts = [p for p in real.split(os.sep) if p]
    if "block" in parts:
        i = parts.index("block")
        if i + 1 < len(parts):
            return parts[i + 1]
    return os.path.basename(real) or None


def read_cpu_temp():
    """返回 (温度 or None, 来源说明)

    优先级：
      1. hwmon 的 coretemp / k10temp 等 —— **按 tempN_label 挑，不按序号**
      2. /sys/class/thermal/thermal_zone* —— **跳过 acpitz**（ACPI 假值）
      3. sensors 命令 —— 最后兜底
    """
    # 1) hwmon 优先
    for d, chip, chip_l in _hwmon_chips():
        if not any(k in chip_l for k in CPU_HWMON_CHIPS):
            continue
        items = []  # [(label, idx, value)]
        try:
            files = os.listdir(d)
        except Exception:
            continue
        for fn in files:
            m = re.match(r"temp(\d+)_input$", fn)
            if not m:
                continue
            idx = m.group(1)
            val = _read_millideg(os.path.join(d, fn))
            if val is None:
                continue
            label = ""
            try:
                with open(os.path.join(d, "temp%s_label" % idx), "r") as f:
                    label = f.read().strip()
            except Exception:
                pass
            items.append((label, idx, val))
        if not items:
            continue
        # 按 label 偏好挑
        for kw in CPU_LABEL_PREFER:
            for label, idx, val in items:
                if kw in label.lower():
                    return val, "hwmon:%s temp%s(%s)" % (chip, idx, label)
        # label 都识别不出来 -> 取所有传感器最大值（比盲选序号安全）
        label, idx, val = max(items, key=lambda x: x[2])
        return val, "hwmon:%s temp%s(无label,取最大)" % (chip, idx)

    # 2) thermal_zone（明确排除 acpitz）
    tbase = THERMAL_BASE
    try:
        zones = sorted(os.listdir(tbase))
    except Exception:
        zones = []
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        zdir = os.path.join(tbase, z)
        try:
            with open(os.path.join(zdir, "type"), "r") as f:
                ztype = f.read().strip()
        except Exception:
            ztype = ""
        if "acpitz" in ztype.lower():
            continue          # ACPI 热区常年恒定的假值，直接跳过
        val = _read_millideg(os.path.join(zdir, "temp"))
        if val is not None:
            return val, "thermal:%s(%s)" % (z, ztype or "?")

    # 3) sensors 兜底
    try:
        out = subprocess.run(["sensors"], capture_output=True, text=True,
                             timeout=10).stdout
        temps = [int(x) for x in re.findall(r"\+(\d+)\.\d+°C", out)]
        if temps:
            return max(temps), "sensors(取最大)"
    except Exception:
        pass
    return None, "未找到可用传感器"


def get_cpu_temp():
    """兼容旧调用的简版：只返回温度"""
    return read_cpu_temp()[0]


def _list_disks(blacklist):
    disks = []
    try:
        out = subprocess.run(["lsblk", "-dn", "-o", "NAME,TYPE"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "disk":
                name = parts[0]
                if name.startswith(("loop", "zram", "sr")):
                    continue
                if name in blacklist:
                    continue
                disks.append(name)
    except Exception as e:
        log("lsblk 执行失败: %s" % e)
    return disks


def _disk_sleeping(dev):
    """判断硬盘是否休眠，避免唤醒"""
    try:
        r = subprocess.run(["hdparm", "-C", dev], capture_output=True,
                           text=True, timeout=10)
        txt = (r.stdout or "").lower()
        if "standby" in txt or "sleeping" in txt:
            return True
    except Exception:
        pass
    return False


def _smart_temp(dev):
    """smartctl 读温度。优先精确匹配 ATA 属性 194 / NVMe Temperature 行。

    别用「任意含 temperature 的行 + 取最后一个数字」这种模糊匹配 ——
    有些盘的 SMART 表里同时有 190 Airflow_Temperature_Cel 和 194 Temperature_Celsius，
    模糊匹配可能取到前者（机箱进风温度，不是盘温）。
    """
    try:
        r = subprocess.run(["smartctl", "-A", "-n", "standby", dev],
                           capture_output=True, text=True, timeout=15)
        out = r.stdout or ""
    except Exception:
        return None

    # ATA：194 Temperature_Celsius
    for line in out.splitlines():
        if re.match(r"^\s*194\s", line) and "temperature" in line.lower():
            nums = re.findall(r"(\d+)", line)
            if nums:
                val = int(nums[-1])
                if 0 < val < 100:
                    return val
    # NVMe：Temperature:  40 Celsius
    m = re.search(r"^\s*Temperature:\s+(\d+)\s+Celsius", out, re.M)
    if m:
        val = int(m.group(1))
        if 0 < val < 100:
            return val
    # 兜底：任意含 temperature 的行取最后一个数字
    for line in out.splitlines():
        if "temperature" in line.lower():
            nums = re.findall(r"(\d+)", line)
            if nums:
                val = int(nums[-1])
                if 0 < val < 100:
                    return val
    return None


def read_hdd_temp(blacklist=None):
    """返回 (温度 or None, 来源说明)

    优先级：
      1. hwmon 的 drivetemp —— 内核直读，**不唤醒休眠中的盘**
      2. smartctl -A -n standby —— 兜底
    多盘时取最高温（最保守）。
    """
    blacklist = blacklist or []
    best = None
    best_name = None

    # 1) hwmon drivetemp
    for d, chip, chip_l in _hwmon_chips():
        if "drivetemp" not in chip_l:
            continue
        name = _block_name_from_device(os.path.join(d, "device"))
        if name and name in blacklist:
            continue
        val = _read_millideg(os.path.join(d, "temp1_input"))
        if val is not None and (best is None or val > best):
            best = val
            best_name = name or chip
    if best is not None:
        return best, "drivetemp:%s" % best_name

    # 2) smartctl 兜底
    for name in _list_disks(blacklist):
        dev = "/dev/" + name
        if _disk_sleeping(dev):
            continue
        t = _smart_temp(dev)
        if t is not None and (best is None or t > best):
            best = t
            best_name = name
    if best is not None:
        return best, "smartctl:%s" % best_name
    return None, "无有效数据"


def get_hdd_temp(blacklist=None):
    """兼容旧调用的简版：只返回温度"""
    return read_hdd_temp(blacklist)[0]


# ---------------------------------------------------------------------------
# 风扇控制
# ---------------------------------------------------------------------------
def resolve_fanctrl():
    if os.path.isfile(FANCTRL) and os.access(FANCTRL, os.X_OK):
        return FANCTRL
    if os.path.isfile(FANCTRL_FALLBACK) and os.access(FANCTRL_FALLBACK, os.X_OK):
        return FANCTRL_FALLBACK
    return None


def set_fan_speed(speed: int):
    """下发转速，返回 (成功布尔, 错误信息)"""
    binary = resolve_fanctrl()
    if not binary:
        return False, "未找到 fanctrl 可执行文件"
    speed = max(0, min(100, int(speed)))
    try:
        r = subprocess.run([binary, str(speed)], capture_output=True,
                           text=True, timeout=15)
        if r.returncode == 0:
            return True, None
        return False, "fanctrl 退出码 %d: %s" % (r.returncode, (r.stderr or "").strip())
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 曲线计算
# ---------------------------------------------------------------------------
def _int_or(value, default):
    """配置项可能是 None / 字符串 / 空值，统一转成 int"""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _speed_for(value, key, cfg, fallback):
    """单侧查表（无迟滞版，保留给测试与兼容）：返回该侧需要的转速"""
    if value is None:
        return None
    for tier in cfg["curves"]:
        if value <= tier.get(key, 999):
            return tier.get("speed", fallback)
    return cfg.get("max_speed", fallback)


def _naive_tier(temp, key, tiers):
    """不带迟滞的档位索引"""
    for i, tier in enumerate(tiers):
        if temp <= tier.get(key, 999):
            return i
    return len(tiers) - 1


def _tier_index(temp, key, tiers, cur_idx, hyst):
    """带迟滞的档位索引 —— 消除「温度骑在分界线上反复翻档」

    规则（以 CPU 档位 45/60/68/78/999、迟滞 3℃ 为例）：
      · 当前在第 2 档（上界 60）时，温度要 **>63℃** 才会升到第 3 档；
      · 当前在第 3 档（上一档上界 60）时，温度要 **<57℃** 才会降回第 2 档。
    中间 57~63℃ 是死区，保持现状 —— 这正是消灭抖动的关键。
    """
    if temp is None:
        return None
    n = len(tiers)
    if cur_idx is None or not (0 <= cur_idx < n):
        return _naive_tier(temp, key, tiers)
    i = cur_idx
    while i + 1 < n and temp > tiers[i].get(key, 999) + hyst:
        i += 1
    while i > 0 and temp < tiers[i - 1].get(key, 999) - hyst:
        i -= 1
    return i


def _clamp_step(cur, new, step):
    """渐变限速：单次最多变化 step 档"""
    if cur is None or new is None or step <= 0:
        return new
    if new > cur + step:
        return cur + step
    if new < cur - step:
        return cur - step
    return new


def calc_target_speed(cfg: dict, cpu_temp, hdd_temp) -> int:
    """无迟滞的静态查表版（兼容旧调用）

    CPU 与硬盘分别查表，取两者中更高的转速需求（谁热听谁的）。
    """
    speeds = []
    cpu_speed = _speed_for(cpu_temp, "cpu", cfg, 100)
    hdd_speed = _speed_for(hdd_temp, "hdd", cfg, 100)
    if cpu_speed is not None:
        speeds.append(cpu_speed)
    if hdd_speed is not None:
        speeds.append(hdd_speed)
    if not speeds:
        return cfg.get("max_speed", 100)
    target = max(speeds)
    return max(cfg.get("min_speed", 0), min(cfg.get("max_speed", 100), target))


def decide_speed(cfg: dict, cpu_temp, hdd_temp, cpu_idx=None, hdd_idx=None):
    """带迟滞 + 渐变 + 紧急直通的决策函数

    返回 (目标转速, CPU 档位, 硬盘档位)。

    cpu_idx / hdd_idx 是上一次决策留下的档位（存在 state.json 里），
    迟滞和渐变都必须依赖它 —— 只看单次温度是做不到的。
    """
    tiers = cfg.get("curves") or DEFAULT_CONFIG["curves"]
    n = len(tiers)
    hyst = _int_or(cfg.get("hysteresis"), HYSTERESIS_DEFAULT)
    step = _int_or(cfg.get("slew_step"), SLEW_STEP_DEFAULT)
    emerg_cpu = _int_or(cfg.get("emergency_cpu"), EMERGENCY_CPU_DEFAULT)
    emerg_hdd = _int_or(cfg.get("emergency_hdd"), EMERGENCY_HDD_DEFAULT)

    cpu_i = _tier_index(cpu_temp, "cpu", tiers, cpu_idx, hyst)
    hdd_i = _tier_index(hdd_temp, "hdd", tiers, hdd_idx, hyst)

    # 上一次的档位可能因为用户改过曲线而越界，先做合法性检查
    cpu_prev = cpu_idx if isinstance(cpu_idx, int) and 0 <= cpu_idx < n else None
    hdd_prev = hdd_idx if isinstance(hdd_idx, int) and 0 <= hdd_idx < n else None

    # 紧急直通：真过热时无视迟滞与渐变，立刻满速
    emergency = False
    if cpu_temp is not None and cpu_temp >= emerg_cpu:
        cpu_i = n - 1
        emergency = True
    if hdd_temp is not None and hdd_temp >= emerg_hdd:
        hdd_i = n - 1
        emergency = True

    if not emergency:
        cpu_i = _clamp_step(cpu_prev, cpu_i, step)
        hdd_i = _clamp_step(hdd_prev, hdd_i, step)

    # 落库前把档位索引收敛到合法范围
    if cpu_i is not None:
        cpu_i = max(0, min(n - 1, cpu_i))
    if hdd_i is not None:
        hdd_i = max(0, min(n - 1, hdd_i))

    speeds = []
    for i in (cpu_i, hdd_i):
        if i is not None:
            speeds.append(tiers[i].get("speed", cfg.get("max_speed", 100)))
    if not speeds:
        return cfg.get("max_speed", 100), cpu_i, hdd_i
    target = max(speeds)
    lo = _int_or(cfg.get("min_speed"), 0)
    hi = _int_or(cfg.get("max_speed"), 100)
    return max(lo, min(hi, target)), cpu_i, hdd_i


# ---------------------------------------------------------------------------
# 单次巡检
# ---------------------------------------------------------------------------
def tick(cfg: dict) -> None:
    cpu, cpu_src = read_cpu_temp()
    hdd, hdd_src = read_hdd_temp(cfg.get("hdd_devices_blacklist"))

    st = read_state()
    current = st.get("current_speed")

    if cpu is None and hdd is None:
        log("无法获取任何温度数据，跳过本次调整 (CPU源:%s / HDD源:%s)"
            % (cpu_src, hdd_src))
        write_state(cpu_temp=cpu, hdd_temp=hdd, last_error="温度读取失败",
                    cpu_source=cpu_src, hdd_source=hdd_src)
        return

    # 带上一次档位做迟滞/渐变决策（只看单次温度做不到）
    target, cpu_i, hdd_i = decide_speed(
        cfg, cpu, hdd, st.get("cpu_tier"), st.get("hdd_tier"))

    if current != target:
        ok, err = set_fan_speed(target)
        if ok:
            log("转速调整: %s%% -> %s%% (CPU:%s°C HDD:%s°C)"
                % (current, target, cpu if cpu is not None else "未知",
                   hdd if hdd is not None else "未知"))
            write_state(current_speed=target, last_action="温控曲线调整",
                        last_change=time.time(), last_error=None)
        else:
            log("设置转速 %s%% 失败: %s" % (target, err))
            write_state(last_error=err)
    else:
        log("CPU:%s°C HDD:%s°C 转速:%s%% (无需调整)"
            % (cpu if cpu is not None else "未知",
               hdd if hdd is not None else "未知", current))

    write_state(cpu_temp=cpu, hdd_temp=hdd, target_speed=target,
                cpu_source=cpu_src, hdd_source=hdd_src,
                cpu_tier=cpu_i, hdd_tier=hdd_i)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def acquire_lock():
    """获取单例文件锁。已有实例在跑时返回 None，调用方应直接退出。

    作用：应用可能被多处拉起（systemd 服务 + 应用的 cmd/main），
    没有互斥会导致两个守护进程同时调 fanctrl、同时写日志。
    """
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        fh = open(LOCK_FILE, "w")
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh  # 持有引用，进程存活期间不释放
    except (IOError, OSError):
        return None


def main():
    # 单例保护：拿不到锁说明已有实例在跑，直接退出
    lock = acquire_lock()
    if lock is None:
        print("已有 fand 实例在运行，本次启动退出", flush=True)
        return

    log("=" * 50)
    log("风扇温控守护进程启动 (pid=%d)" % os.getpid())
    log("APPDIR=%s" % APPDIR)
    log("fanctrl=%s" % (resolve_fanctrl() or "未找到"))

    save_default_config_if_missing()

    # 首次启动：把转速拉到曲线基准（最低档转速），避免停留在 BIOS 默认
    cfg = load_config()

    # 启动时探测一次传感器来源，写进日志 —— 排查「读到的值不对」时第一眼就看这里
    _ct, _cs = read_cpu_temp()
    _ht, _hs = read_hdd_temp(cfg.get("hdd_devices_blacklist"))
    log("温度传感器: CPU=%s -> %s | HDD=%s -> %s"
        % (_cs, ("%s°C" % _ct) if _ct is not None else "读取失败",
           _hs, ("%s°C" % _ht) if _ht is not None else "读取失败"))
    log("平滑策略: 迟滞 %s℃ | 单次最多 %s 档 | 紧急直通 CPU≥%s℃ / HDD≥%s℃"
        % (cfg.get("hysteresis"), cfg.get("slew_step"),
           cfg.get("emergency_cpu"), cfg.get("emergency_hdd")))

    st = read_state()
    if st.get("current_speed") is None:
        base = cfg["curves"][0].get("speed", 30) if cfg["curves"] else 30
        ok, err = set_fan_speed(base)
        if ok:
            log("初始化转速为 %s%%" % base)
            write_state(current_speed=base, last_action="初始化")
        else:
            log("初始化转速失败: %s" % err)

    last_cfg_mtime = 0
    while True:
        try:
            # 每次循环重新读配置，实现页面改参数即时生效
            try:
                mtime = os.path.getmtime(CONFIG_FILE)
            except OSError:
                mtime = 0
            if mtime != last_cfg_mtime:
                cfg = load_config()
                last_cfg_mtime = mtime

            if cfg.get("enabled", True):
                tick(cfg)
            else:
                log("温控已暂停（页面关闭了守护开关）")
                write_state(paused=True)

            interval = max(5, int(cfg.get("check_interval", 30)))
            time.sleep(interval)
        except KeyboardInterrupt:
            log("收到中断信号，退出")
            break
        except Exception as e:
            log("主循环异常: %s" % e)
            time.sleep(10)


if __name__ == "__main__":
    main()
