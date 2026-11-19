#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风扇温控 Web 后端 — 飞牛 fnOS 应用 FanControl

零第三方依赖（仅 Python 标准库 http.server）。
提供 REST API + 静态页面，监听 manifest 声明的 service_port。

API:
  POST /api/login            登录（密码校验，返回 token）
  GET  /api/status           状态与温度
  POST /api/speed            手动设定转速 {"speed": 35}
  GET  /api/config           读取温控配置
  POST /api/config           保存温控配置
  GET  /api/log?lines=200    读取日志尾部
  POST /api/log/clear        清空日志
  POST /api/autostart        开机自启开关 {"enabled": true}
  GET  /api/autostart        查询自启状态
  GET  /api/power            定时开关机：计划、下次执行、RTC 闹钟状态
  POST /api/power            保存定时计划
  POST /api/power/sync       立即把下一次开机写进主板 RTC
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
APPDIR = os.environ.get("FAN_APPDIR", "/var/apps/FanControl/target")
PKGECT = os.environ.get("FAN_PKGETC", "/var/apps/FanControl/etc")
PKGVAR = os.environ.get("FAN_PKGVAR", "/var/apps/FanControl/var")
WEBROOT = os.path.join(APPDIR, "www")

CONFIG_FILE = os.path.join(PKGECT, "config.json")
AUTH_FILE = os.path.join(PKGECT, "auth.json")
STATE_FILE = os.path.join(PKGVAR, "state.json")
LOG_FILE = os.path.join(PKGVAR, "fan_control.log")

FANCTRL = os.path.join(APPDIR, "bin", "fanctrl")
FANCTRL_FALLBACK = "/root/fanctrl"
FAND_SCRIPT = os.path.join(APPDIR, "fand.py")

PORT = int(os.environ.get("TRIM_SERVICE_PORT") or os.environ.get("FAN_PORT") or 7300)
HOST = "0.0.0.0"

# 登录 token 有效期（秒）
TOKEN_TTL = 7 * 24 * 3600
_tokens = {}   # token -> 过期时间戳
_tokens_lock = threading.Lock()

DEFAULT_PASSWORD = "fancontrol"


# ---------------------------------------------------------------------------
# 通用读写
# ---------------------------------------------------------------------------
def _read_json(path, default=None):
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] [web] %s" % (ts, msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 密码 / 鉴权
# ---------------------------------------------------------------------------
def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), 120000)
    return {"salt": salt, "hash": digest.hex()}


def ensure_auth_file():
    if not os.path.isfile(AUTH_FILE):
        _write_json(AUTH_FILE, _hash_password(DEFAULT_PASSWORD))
        log("已生成默认密码文件（默认密码: %s）" % DEFAULT_PASSWORD)


def auth_enabled():
    """是否启用访问密码。auth.json 里标了 disabled 就是「不设密码」，免登录。"""
    return not bool(_read_json(AUTH_FILE).get("disabled"))


def verify_password(password):
    if not auth_enabled():
        return True
    data = _read_json(AUTH_FILE)
    if not data.get("salt") or not data.get("hash"):
        return password == DEFAULT_PASSWORD
    calc = _hash_password(password, data["salt"])
    return hmac.compare_digest(calc["hash"], data["hash"])


def change_password(old, new):
    """new 留空 = 取消密码。返回 (ok, err)。

    当前没设密码时不再校验 old —— 那正是「首次设密码」的路径；
    设了密码则必须验原密码，避免别人在已登录的浏览器上顺手改掉。
    """
    new = (new or "").strip()
    enabled = auth_enabled()
    if enabled and not verify_password(old or ""):
        return False, "原密码错误"
    if new:
        if len(new) < 4:
            return False, "新密码至少 4 位"
        _write_json(AUTH_FILE, _hash_password(new))
        log("访问密码已更新")
    else:
        # 只写标记，不删文件：删掉会被 ensure_auth_file 重新种回默认密码
        _write_json(AUTH_FILE, {"disabled": True})
        log("访问密码已取消（页面免登录）")
    with _tokens_lock:
        _tokens.clear()
    return True, None


def issue_token():
    tok = secrets.token_urlsafe(32)
    with _tokens_lock:
        _tokens[tok] = time.time() + TOKEN_TTL
    return tok


def check_token(tok):
    if not tok:
        return False
    with _tokens_lock:
        exp = _tokens.get(tok)
        if not exp:
            return False
        if exp < time.time():
            del _tokens[tok]
            return False
        return True


# ---------------------------------------------------------------------------
# 业务逻辑
# ---------------------------------------------------------------------------
def resolve_fanctrl():
    for p in (FANCTRL, FANCTRL_FALLBACK):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# 打包时与 manifest 的 version 保持一致（发版时两处一起改）。
# manifest 在真机上不一定随 target 落地，读不到就拿它顶上，页面永远有版本号。
BUILD_VERSION = "1.9.4"

_VER_CACHE = {"ts": 0.0, "v": "", "src": ""}


def _manifest_dirs():
    """manifest 可能落地的几个位置，按可信度从高到低排。

    真机上 app.tgz 解到 target/ 下，manifest 是包元数据 —— 有的系统会把它
    一起放到应用根，有的只留在 .fpk 里不放出来。所以这里多点探测，
    最后兜底用打包时写死的 BUILD_VERSION。
    """
    out = []
    for p in (os.path.dirname(APPDIR.rstrip("/")),
              os.path.dirname(PKGECT.rstrip("/")),
              os.path.dirname(PKGVAR.rstrip("/")),
              APPDIR):
        if p and p not in out:
            out.append(p)
    return out


def app_version():
    """读 manifest 里的 version，读不到就用 BUILD_VERSION 兜底。

    页面「关于」区显示它，所以绝不能返回空串 —— 那看起来就像版本号丢了。
    """
    now = time.time()
    if _VER_CACHE["v"] and now - _VER_CACHE["ts"] < 60:
        return _VER_CACHE["v"]
    for d in _manifest_dirs():
        p = os.path.join(d, "manifest")
        try:
            # utf-8-sig：兼容被记事本另存时带上 BOM 的 manifest
            with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    if line.strip().startswith("version"):
                        v = line.split("=", 1)[-1].strip()
                        if v:
                            _VER_CACHE.update(ts=now, v=v, src="manifest")
                            return v
        except Exception:
            continue
    _VER_CACHE.update(ts=now, v=BUILD_VERSION, src="builtin")
    return BUILD_VERSION


def read_state():
    return _read_json(STATE_FILE, {})


def read_config():
    sys.path.insert(0, APPDIR)
    try:
        import fand
        return fand.load_config()
    except Exception:
        return _read_json(CONFIG_FILE, {})


def save_config(cfg):
    _write_json(CONFIG_FILE, cfg)


def get_cpu_temp_now():
    """返回 (温度 or None, 来源说明)"""
    sys.path.insert(0, APPDIR)
    try:
        import fand
        return fand.read_cpu_temp()
    except Exception:
        return None, "读取失败"


def get_hdd_temp_now(blacklist=None):
    """返回 (温度 or None, 来源说明)"""
    sys.path.insert(0, APPDIR)
    try:
        import fand
        return fand.read_hdd_temp(blacklist)
    except Exception:
        return None, "读取失败"


def fand_running():
    try:
        r = subprocess.run(["pgrep", "-f", "fand.py"], capture_output=True,
                           text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def power_mod():
    """延迟导入定时开关机模块（与 fand.py 共用同一份实现）"""
    sys.path.insert(0, APPDIR)
    import powersched
    return powersched


def energy_mod():
    """延迟导入功耗统计模块"""
    sys.path.insert(0, APPDIR)
    import energy
    return energy


def netflow_mod():
    """延迟导入流量统计模块"""
    sys.path.insert(0, APPDIR)
    import netflow
    return netflow


def ups_mod():
    """延迟导入 UPS 读取模块"""
    sys.path.insert(0, APPDIR)
    import ups
    return ups


def notify_mod():
    """延迟导入消息推送模块"""
    sys.path.insert(0, APPDIR)
    import notify
    return notify


def service_enabled():
    """查询 systemd 自启状态"""
    try:
        r = subprocess.run(["systemctl", "is-enabled", "fan-control.service"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "enabled"
    except Exception:
        return False


def tail_log(lines=200):
    try:
        if not os.path.isfile(LOG_FILE):
            return ""
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return "".join(data[-lines:])
    except Exception as e:
        return "日志读取失败: %s" % e


# ---------------------------------------------------------------------------
# 系统自启控制
# ---------------------------------------------------------------------------
SYSTEMD_UNIT = "/etc/systemd/system/fan-control.service"


def _write_systemd_unit():
    unit = """[Unit]
Description=Fan Control Temperature Daemon (fnOS FanControl app)
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 {fand}
Restart=always
RestartSec=30
User=root
WorkingDirectory={appdir}
Environment=FAN_APPDIR={appdir}
Environment=FAN_PKGETC={etc}
Environment=FAN_PKGVAR={var}

[Install]
WantedBy=multi-user.target
""".format(fand=FAND_SCRIPT, appdir=APPDIR, etc=PKGECT, var=PKGVAR)
    os.makedirs(os.path.dirname(SYSTEMD_UNIT), exist_ok=True)
    with open(SYSTEMD_UNIT, "w", encoding="utf-8") as f:
        f.write(unit)


def set_autostart(enabled):
    try:
        if enabled:
            _write_systemd_unit()
            subprocess.run(["systemctl", "daemon-reload"], timeout=15, check=False)
            subprocess.run(["systemctl", "enable", "--now", "fan-control.service"],
                           timeout=30, check=False)
        else:
            subprocess.run(["systemctl", "disable", "--now", "fan-control.service"],
                           timeout=30, check=False)
        return True, None
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FanControl/1.1"
    protocol_version = "HTTP/1.1"     # 支持 keep-alive，桌面 iframe 加载更稳

    # --- 工具方法 -------------------------------------------------------
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_csv(self, text, filename):
        """导出 CSV。带 BOM，否则 Excel 打开中文表头是乱码。"""
        body = ("\ufeff" + text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        """跨域预检 —— 桌面壳可能以不同 origin 内嵌页面"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _auth_ok(self):
        if not auth_enabled():
            return True
        tok = self.headers.get("X-Auth-Token") or ""
        if not tok:
            qs = parse_qs(urlparse(self.path).query)
            tok = (qs.get("token") or [""])[0]
        return check_token(tok)

    def _serve_static(self, path, headers_only=False):
        """提供静态文件。headers_only=True 时只回头部（用于 HEAD 请求）"""
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        # 防目录穿越
        if ".." in rel:
            self._simple_response(400, "Bad Request")
            return
        fp = os.path.join(WEBROOT, rel)
        if not os.path.isfile(fp):
            self._simple_response(404, "Not Found")
            return
        ext = os.path.splitext(fp)[1].lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".ico": "image/x-icon",
            ".webp": "image/webp",
            ".woff2": "font/woff2",
        }.get(ext, "application/octet-stream")
        try:
            size = os.path.getsize(fp)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if not headers_only:
                with open(fp, "rb") as f:
                    self.wfile.write(f.read())
        except Exception:
            self._simple_response(500, "Internal Server Error")

    def _simple_response(self, code, text):
        """无 body（HEAD）或降级错误响应"""
        body = ("%d %s" % (code, text)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # --- 路由 -----------------------------------------------------------
    def do_HEAD(self):
        """飞牛桌面 iframe 会先发 HEAD 探测，必须支持，否则页面被判为不可用"""
        p = urlparse(self.path).path
        self._serve_static(p, headers_only=True)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        qs = parse_qs(u.query)

        if p.startswith("/api/"):
            if p == "/api/ping":
                return self._send_json(200, {"ok": True, "time": time.time()})
            if not self._auth_ok():
                return self._send_json(401, {"ok": False, "error": "未登录或登录已过期"})

            if p == "/api/status":
                return self._status()
            if p == "/api/config":
                return self._send_json(200, {"ok": True, "config": read_config()})
            if p == "/api/log":
                lines = int((qs.get("lines") or ["200"])[0] or 200)
                lines = max(10, min(2000, lines))
                return self._send_json(200, {"ok": True, "log": tail_log(lines)})
            if p == "/api/autostart":
                return self._send_json(200, {
                    "ok": True,
                    "enabled": service_enabled(),
                    "daemon_running": fand_running(),
                })
            if p == "/api/power":
                try:
                    return self._send_json(200, power_mod().status())
                except Exception as e:
                    return self._send_json(500, {"ok": False, "error": str(e)})
            if p == "/api/ups":
                # UPS 探测：给页面「检测」按钮用。失败也返回 200 + ok:false，
                # 前端好把原因显示出来，而不是当成接口挂了。
                try:
                    c = energy_mod().conf(read_config())
                    return self._send_json(200, ups_mod().summary(c["ups"]))
                except Exception as e:
                    return self._send_json(200, {"ok": False, "error": str(e)})
            if p == "/api/energy" or p == "/api/traffic":
                try:
                    days = int((qs.get("days") or ["30"])[0] or 30)
                except (TypeError, ValueError):
                    days = 30
                days = max(1, min(90, days))
                try:
                    mod = energy_mod() if p == "/api/energy" else netflow_mod()
                    return self._send_json(200, mod.summary(read_config(), days))
                except Exception as e:
                    log("统计接口失败 %s: %s" % (p, e))
                    return self._send_json(500, {"ok": False, "error": str(e)})
            if p == "/api/export/energy.csv" or p == "/api/export/traffic.csv":
                try:
                    if p.endswith("energy.csv"):
                        text = energy_mod().export_csv(read_config(), 45)
                        name = "fancontrol-energy.csv"
                    else:
                        text = netflow_mod().export_csv(read_config(), 45)
                        name = "fancontrol-traffic.csv"
                    return self._send_csv(text, name)
                except Exception as e:
                    return self._send_json(500, {"ok": False, "error": str(e)})
            if p == "/api/notify":
                try:
                    nm = notify_mod()
                    cfg = read_config()
                    c = nm.conf(cfg)
                    res = nm.last_result()
                    return self._send_json(200, {
                        "ok": True,
                        "enabled": c["enabled"],
                        "configured": bool(c["base_url"] and c["token"]),
                        "channels": c["channels"],
                        "schedule": c["schedule"],
                        "threshold": c["threshold"],
                        "last": res.get("last") or {},
                        "threshold_ts": res.get("threshold_ts"),
                        "recent_slots": res.get("slots") or [],
                        "now": time.time(),
                    })
                except Exception as e:
                    log("推送状态接口失败: %s" % e)
                    return self._send_json(500, {"ok": False, "error": str(e)})
            return self._send_json(404, {"ok": False, "error": "未知接口"})

        return self._serve_static(p)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path

        if p == "/api/login":
            if not auth_enabled():
                return self._send_json(200, {"ok": True, "token": issue_token(),
                                             "no_password": True})
            body = self._read_body()
            pw = body.get("password") or ""
            if verify_password(pw):
                tok = issue_token()
                log("登录成功")
                return self._send_json(200, {"ok": True, "token": tok})
            log("登录失败：密码错误")
            return self._send_json(401, {"ok": False, "error": "密码错误"})

        if not self._auth_ok():
            return self._send_json(401, {"ok": False, "error": "未登录或登录已过期"})

        if p == "/api/speed":
            body = self._read_body()
            try:
                speed = int(body.get("speed"))
            except Exception:
                return self._send_json(400, {"ok": False, "error": "转速参数无效"})
            speed = max(0, min(100, speed))
            binary = resolve_fanctrl()
            if not binary:
                return self._send_json(500, {"ok": False, "error": "未找到 fanctrl"})
            try:
                r = subprocess.run([binary, str(speed)], capture_output=True,
                                   text=True, timeout=15)
                if r.returncode != 0:
                    return self._send_json(500, {
                        "ok": False,
                        "error": "fanctrl 返回 %d" % r.returncode})
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})
            st = read_state()
            st.update({"current_speed": speed, "manual": True,
                       "last_action": "手动设定 %d%%" % speed,
                       "last_change": time.time(), "updated_at": time.time()})
            _write_json(STATE_FILE, st)
            log("手动设定转速 %d%%" % speed)
            return self._send_json(200, {"ok": True, "speed": speed})

        if p == "/api/config":
            body = self._read_body()
            cfg = body.get("config")
            if not isinstance(cfg, dict):
                return self._send_json(400, {"ok": False, "error": "配置格式错误"})
            # 老缓存页面不认识新增的配置段，整段都不会带。缺哪段就从磁盘上的现有
            # 配置补回来，否则用户点一次「保存曲线」就把统计 / 推送设置悄悄清空了。
            _old = read_config()
            for _k in ("metrics", "notify"):
                if _k not in cfg and isinstance(_old.get(_k), dict):
                    cfg[_k] = _old[_k]
            # 基础校验
            try:
                cfg["check_interval"] = max(5, min(3600, int(cfg.get("check_interval", 30))))
                cfg["min_speed"] = max(0, min(100, int(cfg.get("min_speed", 20))))
                cfg["max_speed"] = max(0, min(100, int(cfg.get("max_speed", 100))))
                curves = []
                for c in cfg.get("curves", []):
                    curves.append({
                        "cpu": max(0, min(999, int(c.get("cpu", 999)))),
                        "hdd": max(0, min(999, int(c.get("hdd", 999)))),
                        "speed": max(0, min(100, int(c.get("speed", 50)))),
                    })
                if not curves:
                    return self._send_json(400, {"ok": False, "error": "至少需要一档曲线"})
                curves.sort(key=lambda x: (x["cpu"], x["hdd"]))
                cfg["curves"] = curves
                cfg["enabled"] = bool(cfg.get("enabled", True))
                bb = cfg.get("boot_boost")
                if not isinstance(bb, dict):
                    bb = {}
                try:
                    bb_sec = max(0, min(3600, int(bb.get("seconds", 60))))
                except (TypeError, ValueError):
                    bb_sec = 60
                try:
                    bb_up = max(60, min(86400, int(bb.get("max_uptime", 600))))
                except (TypeError, ValueError):
                    bb_up = 600
                cfg["boot_boost"] = {
                    "enabled": bool(bb.get("enabled", True)),
                    "seconds": bb_sec,
                    "max_uptime": bb_up,
                }
                # 流量与功耗统计
                mm = cfg.get("metrics") if isinstance(cfg.get("metrics"), dict) else {}
                pp = mm.get("power") if isinstance(mm.get("power"), dict) else {}
                tt = mm.get("traffic") if isinstance(mm.get("traffic"), dict) else {}

                def _num(src, key, dflt, lo, hi):
                    try:
                        return max(lo, min(hi, float(src.get(key, dflt))))
                    except (TypeError, ValueError):
                        return float(dflt)

                try:
                    mi = max(10, min(600, int(mm.get("interval", 30))))
                except (TypeError, ValueError):
                    mi = 30
                us = pp.get("ups") if isinstance(pp.get("ups"), dict) else {}
                src_pref = str(pp.get("source") or "auto").strip().lower()
                if src_pref not in ("auto", "ups", "rapl", "estimate"):
                    src_pref = "auto"
                cfg["metrics"] = {
                    "enabled": bool(mm.get("enabled", True)),
                    "interval": mi,
                    "power": {
                        "rate_yuan": round(_num(pp, "rate_yuan", 0.6, 0.0, 20.0), 4),
                        "price_periods": energy_mod().sanitize_periods(pp.get("price_periods")),
                        "tdp_w": round(_num(pp, "tdp_w", 65, 1, 1000), 1),
                        "disk_typical_w": round(_num(pp, "disk_typical_w", 8, 0, 60), 1),
                        "nic_fixed_w": round(_num(pp, "nic_fixed_w", 5, 0, 60), 1),
                        "base_w": round(_num(pp, "base_w", 8, 0, 120), 1),
                        "source": src_pref,
                        "ups": {
                            "host": str(us.get("host") or "").strip()[:128],
                            "port": int(_num(us, "port", 0, 0, 65535)),
                            "name": str(us.get("name") or "").strip()[:64],
                        },
                    },
                    "traffic": {
                        "exclude_bridge": bool(tt.get("exclude_bridge", True)),
                        "containers": bool(tt.get("containers", True)),
                    },
                }
                # 消息推送 —— 统一走 notify 模块的规整函数，避免两处各写一份口径。
                # 没带这一段（老缓存页面）就原样保留，不能被默认值冲成「关闭」。
                if isinstance(cfg.get("notify"), dict):
                    nc = notify_mod().conf(cfg)
                    cfg["notify"] = {
                        "enabled": nc["enabled"],
                        "base_url": nc["base_url"],
                        "token": nc["token"],
                        "channels": nc["channels"],
                        "schedule": nc["schedule"],
                        "threshold": nc["threshold"],
                    }
            except Exception as e:
                return self._send_json(400, {"ok": False, "error": "参数错误: %s" % e})
            save_config(cfg)
            log("配置已更新")
            return self._send_json(200, {"ok": True, "config": cfg})

        if p == "/api/log/clear":
            try:
                open(LOG_FILE, "w").close()
                return self._send_json(200, {"ok": True})
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

        if p == "/api/autostart":
            body = self._read_body()
            enabled = bool(body.get("enabled"))
            ok, err = set_autostart(enabled)
            if ok:
                log("开机自启已%s" % ("开启" if enabled else "关闭"))
                return self._send_json(200, {"ok": True, "enabled": enabled})
            return self._send_json(500, {"ok": False, "error": err})

        if p == "/api/power":
            body = self._read_body()
            plan = body.get("plan")
            if not isinstance(plan, dict):
                return self._send_json(400, {"ok": False, "error": "计划格式错误"})
            try:
                pm = power_mod()
                saved = pm.save_plan(plan)
                result = pm.sync_rtc(saved, force=True)
                out = pm.status(saved)
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})
            log("定时开关机计划已更新（%d 条）" % len(saved.get("items") or []))
            out["rtc_sync"] = result
            return self._send_json(200, out)

        if p == "/api/power/sync":
            try:
                pm = power_mod()
                result = pm.sync_rtc(force=True)
                out = pm.status()
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})
            log("手动同步 RTC 闹钟: %s"
                % (result.get("error") or result.get("action") or "无变化"))
            out["rtc_sync"] = result
            return self._send_json(200, out)

        if p == "/api/metrics/reset":
            body = self._read_body()
            which = body.get("type") or "all"
            try:
                if which in ("all", "energy"):
                    energy_mod().reset()
                if which in ("all", "traffic"):
                    netflow_mod().reset()
                log("统计历史已清空（%s）" % which)
                return self._send_json(200, {"ok": True})
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

        if p == "/api/notify/test":
            body = self._read_body()
            try:
                nm = notify_mod()
                # 允许带未保存的地址 / 令牌先试一次：填完立刻验证，不用先存再试
                ov = {}
                for k in ("base_url", "token", "channels"):
                    if k in body:
                        ov[k] = body.get(k)
                r = nm.test(read_config(), ov)
                log("推送测试：%s%s" % ("成功" if r.get("ok") else "失败",
                                       "" if r.get("ok") else " —— %s" % r.get("error")))
                return self._send_json(200 if r.get("ok") else 502, {
                    "ok": bool(r.get("ok")),
                    "sent": r.get("sent") or [],
                    "error": r.get("error") or "",
                })
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

        if p == "/api/password":
            body = self._read_body()
            ok, err = change_password(body.get("old") or "", body.get("new") or "")
            if ok:
                return self._send_json(200, {"ok": True,
                                             "auth_enabled": auth_enabled()})
            return self._send_json(400, {"ok": False, "error": err})

        return self._send_json(404, {"ok": False, "error": "未知接口"})

    # --- 状态聚合 -------------------------------------------------------
    def _status(self):
        st = read_state()
        cfg = read_config()
        cpu, cpu_src = get_cpu_temp_now()
        hdd, hdd_src = get_hdd_temp_now(cfg.get("hdd_devices_blacklist"))
        # 兜底：实时读取失败时用状态文件里的缓存值
        if cpu is None:
            cpu = st.get("cpu_temp")
            cpu_src = (st.get("cpu_source") or cpu_src or "未知") + "（缓存）"
        if hdd is None:
            hdd = st.get("hdd_temp")
            hdd_src = (st.get("hdd_source") or hdd_src or "未知") + "（缓存）"
        boost_until = st.get("boost_until") or 0
        remain = int(round(boost_until - time.time())) if boost_until else 0
        return self._send_json(200, {
            "ok": True,
            "current_speed": st.get("current_speed"),
            "target_speed": st.get("target_speed"),
            "cpu_temp": cpu,
            "hdd_temp": hdd,
            "cpu_source": cpu_src,
            "hdd_source": hdd_src,
            "last_action": st.get("last_action"),
            "last_change": st.get("last_change"),
            "updated_at": st.get("updated_at"),
            "last_error": st.get("last_error"),
            "boot_boost_active": remain > 0,
            "boot_boost_remain": remain if remain > 0 else 0,
            "daemon_running": fand_running(),
            "temp_enabled": cfg.get("enabled", True),
            "auth_enabled": auth_enabled(),
            "fanctrl_found": resolve_fanctrl() is not None,
            "version": app_version(),
            "version_src": _VER_CACHE.get("src") or "",
            "now": time.time(),
        })

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏


def main():
    ensure_auth_file()
    os.makedirs(WEBROOT, exist_ok=True)
    log("Web 服务启动，监听 %s:%d" % (HOST, PORT))
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)

    def _shutdown(signum, frame):
        log("收到信号 %s，关闭服务" % signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, _shutdown)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log("Web 服务已停止")


if __name__ == "__main__":
    main()
