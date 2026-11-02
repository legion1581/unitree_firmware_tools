#!/usr/bin/env python3
"""
webapp.py — browser UI for unitree_firmware_tools.

Run it on the robot (as root) and open the printed address on any LAN device:

    ./start.sh                        # web UI (this) — default
    python3 webapp.py --port 8080
    ./start.sh --reset-password       # forgot the password: back to the default
    ./start.sh --cli                  # the old terminal menu (app.py)

A thin, stdlib-only wrapper over the same feature code the CLI uses
(features/secondary_dev.py, supervisor.py, webservice.py, services.py,
core/selector.py, platforms.detect). No extra dependencies, so it works on an
offline robot.

Login is by password (default "robolegion", change it under Settings); a session
lasts one hour. See features/auth.py.
"""
import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config
import platforms
from features import (air_modules, auth, bkstore, flash, mcu_update, network,
                     partitions, prefs, secondary_dev, services, supervisor,
                     updater, webservice)

REPO = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(REPO, "assets", "web", "index.html")
FAVICON = os.path.join(REPO, "assets", "web", "favicon.svg")
COOKIE = "fwtools_session"

# ---- one-job-at-a-time runner ------------------------------------------------
_JOB_LOCK = threading.Lock()
_JOBS = {}
_CURRENT = {"id": None}


class Job:
    """A background operation whose log lines are polled by the browser."""

    def __init__(self, name):
        self.id = secrets.token_hex(8)
        self.name = name
        self.lines = []
        self.done = False
        self.ok = None
        self.error = None
        self.reboot = False       # True if a power-cycle is warranted afterwards

    def log(self, *a):
        self.lines.append(" ".join(str(x) for x in a))

    def snapshot(self):
        return {"id": self.id, "name": self.name, "lines": list(self.lines),
                "done": self.done, "ok": self.ok, "error": self.error,
                "reboot": self.reboot}


_AUTOBUILD = {"tried": False}


def maybe_autobuild(robot):
    """On first run, kick off the backup-store build once, in the background.
    Go2-only (p8 layout); skipped if one is already built or a job is running."""
    if _AUTOBUILD["tried"] or robot is None or robot.platform != "Go2":
        return
    if not partitions._partitions().get("userdata"):
        _AUTOBUILD["tried"] = True
        return
    cur = _JOBS.get(_CURRENT["id"])
    if cur and not cur.done:                      # don't fight another operation
        return
    try:
        if bkstore.is_built():
            _AUTOBUILD["tried"] = True
            return
    except Exception:
        return
    _AUTOBUILD["tried"] = True
    rid = None
    try:
        info = robot.info()
        rid = {"platform": info.platform, "sn": info.sn, "firmware": info.firmware,
               "edition": info.edition}
    except Exception:
        pass
    start_job("Build partition backups", lambda job: bkstore.build(job.log, robot_info=rid))


def start_job(name, fn):
    """Start fn(job) in a thread. Returns the job, or None if one is running."""
    if not _JOB_LOCK.acquire(blocking=False):
        return None
    job = Job(name)
    _JOBS[job.id] = job
    _CURRENT["id"] = job.id

    def run():
        try:
            job.reboot = bool(fn(job))
            job.ok = True
        except Exception as e:
            job.ok = False
            job.error = str(e)
            job.log(f"ERROR: {e}")
            job.log(traceback.format_exc())
        finally:
            job.done = True
            _JOB_LOCK.release()

    threading.Thread(target=run, daemon=True).start()
    return job


def _after_reply(fn, delay=1.0):
    """Run fn shortly after the current response has gone out."""
    t = threading.Timer(delay, fn)
    t.daemon = False   # request threads are daemons; this must outlive main() on handover
    t.start()


def _stop(httpd):
    httpd.shutdown()
    httpd.server_close()


# ---- secondary development summary -------------------------------------------
def _then_mark(action, reboot):
    """Remember that the robot must be power-cycled after a successful switch."""
    if reboot:
        secondary_dev.mark_power_cycle(action)
    return reboot


def _check(label, value, state, note=""):
    return {"label": label, "value": value, "state": state, "note": note}


def secdev_summary(robot, probe=False):
    """Everything the Secondary development tab shows: on/off, the individual
    checks, and what turning it on/off would do."""
    info = robot.info()
    edition = info.edition
    verdict, files = secondary_dev.status(robot, probe_lowcmd=probe)
    sig = verdict["signals"]
    files = files or []
    count = {v: sum(f["verdict"] == v for f in files) for v in ("installed", "vanilla")}
    patched = count["installed"] > 0
    native = edition == "EDU"
    checks = []

    if native:
        checks.append(_check("Edition", "EDU", "ok", "secondary development is native"))
    elif edition in ("AIR", "PRO", "MAX"):
        checks.append(_check("Edition", edition, "dim", "locked by the stock firmware"))
    else:
        checks.append(_check("Edition", "unknown", "bad",
                             info.extra.get("identity_error") or "could not read the identity"))

    # patched but not yet power-cycled: master_service rewrites these at boot
    pending = "; changes after the power-cycle" if patched and not native else ""
    dds = sig.get("dds")
    checks.append(_check("DDS", {"open": "unsecured", "secured": "secured"}.get(dds, "unknown"),
                         {"open": "ok", "secured": "warn" if pending else "bad"}.get(dds, "dim"),
                         "/unitree/etc/cyclonedds.xml" + ("" if dds else " not readable")
                         + (pending if dds == "secured" else "")))

    ver = sig.get("ver_edition")
    checks.append(_check("ver file", ver or "unknown",
                         "ok" if ver == "EDU" else ("dim" if not ver else "warn" if pending else "bad"),
                         "edition the firmware believes it is (/unitree/robot/basic/ver)"
                         + (pending if ver and ver != "EDU" else "")))

    bs = next((f for f in files if f["name"] == "basic_service"), None)
    if native:
        enc = _check("lowcmd encryption", "not required", "ok", "EDU accepts plaintext rt/lowcmd")
    elif bs and bs["verdict"] == "installed":
        enc = _check("lowcmd encryption", "not required", "ok", "basic_service is patched")
    elif bs and bs["verdict"] == "vanilla":
        enc = _check("lowcmd encryption", "required", "bad", "stock basic_service")
    else:
        enc = _check("lowcmd encryption", "unknown", "dim", "basic_service build not recognised")
    checks.append(enc)

    live = sig.get("lowcmd")
    if probe:
        checks.append(_check("rt/lowcmd on the wire", {
            "plaintext": "plaintext", "encrypted": "encrypted",
            "not_seen": "no publisher", "unavailable": "cannot probe"}.get(live, live),
            {"plaintext": "ok", "encrypted": "bad"}.get(live, "dim"),
            {"not_seen": "nothing is sending lowcmd right now",
             "unavailable": "no cyclonedds CLI on this robot"}.get(live, "3 s sample")))
    else:
        checks.append(_check("rt/lowcmd on the wire", "not probed", "dim",
                             "press the refresh icon to sample it"))

    if files:
        if count["installed"] == len(files):
            checks.append(_check("Patched files", f"{len(files)}/{len(files)}", "ok"))
        elif count["vanilla"] == len(files):
            checks.append(_check("Patched files", f"0/{len(files)}", "dim", "all original"))
        else:
            other = len(files) - count["installed"] - count["vanilla"]
            checks.append(_check("Patched files", f"{count['installed']}/{len(files)}", "warn",
                                 f"{count['vanilla']} original, {other} unrecognised"))

    from core import selector
    fw = info.firmware
    supported = selector.supported_versions(robot.platform)
    pkg = None
    try:
        sel = selector.select(robot)
        if sel:
            pkg = {"version": sel["entry"]["firmware"], "how": sel["how"]}
    except Exception:
        pass
    unsupported = None
    if pkg and pkg["how"] == "version":
        checks.append(_check("Firmware", fw, "ok", f"supported ({robot.platform} {fw} package)"))
    elif pkg:
        checks.append(_check("Firmware", fw or "unknown", "warn",
                             f"not a packaged version; its binaries match the {pkg['version']} package"))
    else:
        unsupported = (f"{robot.platform} firmware {fw or '(unknown)'} is not supported. "
                       f"Supported: {', '.join(supported) or 'none'}.")
        checks.append(_check("Firmware", fw or "unknown", "bad", unsupported))

    backup = None
    try:
        b = secondary_dev.usable_backup(robot)
        backup = b["ts"] if b else None
    except Exception:
        pass
    pending = secondary_dev.power_cycle_pending()
    air = None
    if air_modules.is_air(robot):
        ver = air_modules.catalog_for(robot) or {}      # modules for THIS firmware
        mods = ver.get("modules", [])
        try:
            installed = sum(1 for r in air_modules.status(robot) if r["present"])
        except Exception:
            installed = None
        air = {
            "is_air": True,
            "firmware": robot.firmware_version()[0],
            "installed": installed,
            "count": len(mods),
            "modules": [{"name": m["name"], "version": m["version"], "bytes": m["bytes"]}
                        for m in mods],
            "zip_bytes": ver.get("zip_bytes"),
            "total_bytes": ver.get("total_bytes"),
            "supported": bool(mods),     # False if no v2 package for this firmware
        }
    return {
        # until the power-cycle, show what was switched to (DDS/ver still show the old state)
        "on": (pending["action"] == "on") if pending else (bool(verdict["enabled"]) or patched),
        "pending": pending,
        "native": native and not patched,     # EDU with stock files: nothing to toggle
        "patched": patched,
        "verdict": verdict["verdict"],
        "humanoid": robot.platform in ("R1", "G1"),
        "checks": checks,
        "package": pkg,
        "unsupported": unsupported,
        "backup": backup,
        "air": air,
    }


# ---- system info (Status tab) ------------------------------------------------
def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _fs_usage(path):
    """{used, total} for a mounted path (used = total - free, df-style)."""
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        return {"used": total - st.f_bfree * st.f_frsize, "total": total}
    except OSError:
        return None


def _soc_temp():
    """SoC temperature in °C from the thermal zones (prefer 'soc-thermal'), or None."""
    import glob
    best = None
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        ztype = _read(os.path.join(z, "type")).strip()
        try:
            with open(os.path.join(z, "temp")) as f:
                c = round(int(f.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
        if "soc" in ztype.lower():
            return c
        if best is None:
            best = c
    return best


def _userdata_usage():
    """{used, total, dev} for the p8 userdata ext4, read from its superblock
    (no mount needed). None if it isn't an ext4 partition on this device."""
    try:
        p = partitions._partitions().get("userdata")
        if not p:
            return None
        sb = partitions._ext4_super(p["dev"])
        if not sb:
            return None
        return {"used": sb["fs_used"], "total": sb["fs_size"], "dev": p["dev"]}
    except Exception:
        return None


def system_info():
    up = _read("/proc/uptime").split()
    mem = dict(re.findall(r"(\w+):\s+(\d+) kB", _read("/proc/meminfo")))
    return {
        "hostname": socket.gethostname(),
        "uptime": float(up[0]) if up else None,
        "load": _read("/proc/loadavg").split()[:3],
        "mem_total": int(mem.get("MemTotal", 0)) * 1024,
        "mem_avail": int(mem.get("MemAvailable", 0)) * 1024,
        "rootfs": _fs_usage("/"),          # p7 (/ is the root filesystem)
        "userdata": _userdata_usage(),     # p8
        "kernel": os.uname().release,
        "soc_temp": _soc_temp(),           # °C
        "ips": _lan_ips(),
    }


# ---- console -----------------------------------------------------------------
def console_exec(cmd, timeout=30):
    if not cmd or not cmd.strip():
        return {"rc": 0, "out": ""}
    try:
        timeout = max(1, min(int(timeout or 30), 300))
    except (TypeError, ValueError):
        timeout = 30
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=timeout, cwd="/")
        return {"rc": p.returncode, "out": (p.stdout or "") + (p.stderr or "")}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "out": f"(timed out after {timeout}s)"}
    except OSError as e:
        return {"rc": 127, "out": str(e)}


# ---- request handler ---------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "fwtools/" + config.version()
    robot = None
    service_mode = False

    def log_message(self, fmt, *args):
        pass

    # --- helpers ---
    def _json(self, obj, code=200, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sid(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c[COOKIE].value if COOKIE in c else None

    def _authed(self):
        return auth.session_valid(self._sid())

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # --- routing ---
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            return self._serve_page()
        if u.path in ("/favicon.svg", "/favicon.ico"):
            return self._serve_file(FAVICON, "image/svg+xml", cache="max-age=86400")
        if u.path == "/api/session":
            sid = self._sid()
            if not auth.session_valid(sid):
                return self._json({"ok": False}, 401)
            return self._json({"ok": True, "expires_in": int(auth.session_expiry(sid) - time.time()),
                               "default_password": auth.is_default(), "version": config.version()})
        if not self._authed():
            return self._json({"error": "login required"}, 401)
        try:
            if u.path == "/api/status":
                return self._json(self._status())
            if u.path == "/api/secdev":
                return self._json(secdev_summary(self.robot, probe=qs.get("probe", ["0"])[0] == "1"))
            if u.path == "/api/services":
                return self._json(services.list_services(self.robot.root))
            if u.path == "/api/network":
                return self._json(network.status(self.robot.root))
            if u.path == "/api/backup":
                return self._json(self._backup_listing())
            if u.path.startswith("/api/backup/file/"):
                return self._backup_file(u.path.rsplit("/", 1)[-1])
            if u.path == "/api/backup/all.zip":
                return self._backup_all_zip()
            if u.path == "/api/flash":
                return self._json(flash.status())
            if u.path == "/api/mcu":
                return self._json(mcu_update.status(
                    self.robot, live=qs.get("refresh", ["0"])[0] == "1"))
            if u.path == "/api/update":
                return self._json(updater.check())
            if u.path.startswith("/api/job/"):
                job = _JOBS.get(u.path.rsplit("/", 1)[-1])
                return self._json(job.snapshot() if job else {"error": "no such job"},
                                  200 if job else 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        # the rootfs upload is a raw octet-stream, not JSON
        if u.path == "/api/flash/upload":
            if not self._authed():
                return self._json({"error": "login required"}, 401)
            return self._flash_upload()
        # JSON only: a cross-site <form> can't send it without a CORS preflight
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json({"error": "expected application/json"}, 415)
        body = self._read_body()
        if u.path == "/api/login":
            return self._login(body)
        if u.path == "/api/logout":
            auth.end_session(self._sid())
            return self._json({"ok": True}, headers=[("Set-Cookie", self._cookie("", 0))])
        if not self._authed():
            return self._json({"error": "login required"}, 401)
        try:
            route = {"/api/secdev": self._secdev_toggle,
                     "/api/air_modules": self._air_modules_install,
                     "/api/backup": self._backup_action,
                     "/api/services": self._service_action,
                     "/api/network": self._network,
                     "/api/console": lambda b: self._json(console_exec(b.get("cmd", ""), b.get("timeout"))),
                     "/api/guardian": self._guardian,
                     "/api/webservice": self._webservice,
                     "/api/flash": self._flash_action,
                     "/api/mcu": self._mcu_action,
                     "/api/update": self._update_action,
                     "/api/password": self._password}.get(u.path)
            if route:
                return route(body)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    # --- page + auth ---
    def _serve_page(self):
        return self._serve_file(PAGE, "text/html; charset=utf-8")

    def _serve_file(self, path, ctype, cache="no-store"):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    # --- partition backup store (pre-built images on p8, served from :8080) ---
    def _backup_name(self, what, ext):
        info = self.robot.info()
        parts = [info.platform, info.sn or "unknown-sn", info.firmware or "unknown-fw", what]
        return "_".join(p.replace(" ", "-") for p in parts) + ext

    def _backup_listing(self):
        """The store contents + whether a build is running (job id)."""
        out = bkstore.listing()
        cur = _JOBS.get(_CURRENT["id"])
        out["job"] = cur.id if (cur and not cur.done and cur.name.startswith(("Build", "Re-image"))) else None
        return out

    def _backup_file(self, name):
        """Stream one pre-built image out of the p8 store (Content-Length known)."""
        info = bkstore.file_info(name)
        if not info:
            if flash._P8_BUSY:
                return self._json({"error": "a backup build is in progress; try again shortly"}, 409)
            return self._json({"error": f"{name} is not in the backup store yet"}, 404)
        path, size = info
        area = name[:-len(".img.gz")] if name.endswith(".img.gz") else name
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{self._backup_name(area, ".img.gz")}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        bkstore.stream_file(path, self.wfile)

    def _backup_all_zip(self):
        """Stream every present store image as one .zip (already-gzipped: stored)."""
        files = bkstore.present_files()
        if not files:
            return self._json({"error": "nothing in the backup store yet — build it first"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{self._backup_name("partitions", ".zip")}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()      # no length: streamed, connection closes at the end
        import zipfile
        with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for name, path, _size in files:
                info = zipfile.ZipInfo(name, time.localtime(os.path.getmtime(path))[:6])
                info.compress_type = zipfile.ZIP_STORED
                with open(path, "rb") as src, z.open(info, "w", force_zip64=True) as w:
                    import shutil as _sh
                    _sh.copyfileobj(src, w, 1 << 20)

    def _backup_action(self, body):
        action = body.get("action")
        if action in ("build", "rebuild"):
            info = self.robot.info()
            rid = {"platform": info.platform, "sn": info.sn, "firmware": info.firmware,
                   "edition": info.edition}
            job = start_job("Build partition backups",
                            lambda job: bkstore.build(job.log, robot_info=rid))
            if not job:
                return self._json({"error": "another operation is already running"}, 409)
            return self._json({"job": job.id})
        if action == "backup_one":
            area = body.get("area")
            if not area:
                return self._json({"error": "missing area"}, 400)
            info = self.robot.info()
            rid = {"platform": info.platform, "sn": info.sn, "firmware": info.firmware,
                   "edition": info.edition}
            job = start_job(f"Re-image {area}",
                            lambda job: bkstore.build_one(area, job.log, robot_info=rid))
            if not job:
                return self._json({"error": "another operation is already running"}, 409)
            return self._json({"job": job.id})
        if action == "backup_p7":
            if not body.get("confirm"):
                return self._json({"error": "confirmation required"}, 400)
            lines = []
            try:
                plan = flash.arm(mode="backup", log=lines.append,
                                 debug=bool(body.get("debug")))
            except (ValueError, IOError) as e:
                return self._json({"error": str(e)}, 400)
            _after_reply(lambda: subprocess.run(["systemctl", "reboot"]), delay=1.5)
            return self._json({"ok": True, "rebooting": True, "plan": plan, "lines": lines})
        return self._json({"error": "bad action (expected build|rebuild|backup_one|backup_p7)"}, 400)

    def _mcu_action(self, body):
        """Flash the main MCU to the chosen firmware version (background job)."""
        if body.get("action") != "update":
            return self._json({"error": "bad action (expected update)"}, 400)
        if not body.get("confirm"):
            return self._json({"error": "confirmation required"}, 400)
        version = body.get("version")
        if not version:
            return self._json({"error": "missing version"}, 400)
        info = self.robot.info()
        rid = {"platform": info.platform, "sn": info.sn, "firmware": info.firmware}
        job = start_job(f"Main MCU update {version}",
                        lambda job: mcu_update.update(version, job.log, robot_info=rid))
        if not job:
            return self._json({"error": "another operation is already running"}, 409)
        return self._json({"job": job.id})

    # --- rootfs flash (stage on p8, write p7 via the shutdown pivot) ---
    def _flash_upload(self):
        """Stream a raw request body (the .img.gz) onto p8 as the staged image."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "empty upload (no Content-Length)"}, 400)
        try:
            mp = flash._ensure_stage(rw=True)
        except Exception as e:
            return self._json({"error": f"cannot mount staging: {e}"}, 500)
        # default path: free room by removing the factory linux-rootfs.img first
        freed = 0
        fac = os.path.join(mp, flash.FACTORY_IMG)
        if os.path.isfile(fac):
            freed = os.path.getsize(fac)
            os.remove(fac)
            subprocess.run(["sync"])
        path = os.path.join(mp, flash.STAGE_IMG)
        left = n
        try:
            with open(path, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            subprocess.run(["sync"])
        finally:
            flash._unmount_stage_if_ours()
        if left:
            return self._json({"error": f"short upload: {n - left} of {n} bytes "
                               f"(freed {freed} B from {flash.FACTORY_IMG})"}, 400)
        return self._json({"ok": True, "bytes": n, "name": flash.STAGE_IMG,
                           "replaced_factory": freed})

    def _flash_action(self, body):
        action = body.get("action")
        if action == "verify":
            return self._json(flash.verify(body.get("image", flash.STAGE_IMG),
                                           integrity=bool(body.get("integrity"))))
        if action == "delete_factory":
            lines = []
            removed = flash.delete_factory(log=lines.append)
            return self._json({"ok": True, "removed": removed, "lines": lines,
                               "stage": flash.stage_info()})
        if action == "disarm":
            lines = []
            flash.disarm(log=lines.append)
            return self._json({"ok": True, "lines": lines, "armed": flash.armed()})
        if action == "log":
            return self._json(flash.read_log())
        if action == "metrics":
            return self._json(flash.read_metrics())
        if action == "flash":
            mode = body.get("mode", "dry")
            if mode not in ("dry", "real"):
                return self._json({"error": "mode must be dry|real"}, 400)
            if not body.get("confirm"):
                return self._json({"error": "confirmation required"}, 400)
            if mode == "real" and body.get("confirm") != "FLASH":
                return self._json({"error": "type FLASH to confirm a real write to p7"}, 400)
            lines = []
            # A real flash powers OFF when done (default) so the dog settles to the
            # ground as the motors de-energize and stays down — safer than rebooting
            # straight back into motion. Dry-runs reboot (they write nothing).
            poweroff = mode == "real" and body.get("poweroff", True)
            final = "poweroff" if poweroff else "reboot"
            try:
                plan = flash.arm(mode=mode, image_name=body.get("image", flash.STAGE_IMG),
                                 log=lines.append, require_rootfs=not bool(body.get("force")),
                                 final_action=final)
            except (ValueError, IOError) as e:
                return self._json({"error": str(e)}, 400)
            # trigger the shutdown transition just after this reply; the pivot runs
            # during it and ends by rebooting or powering off per `final`.
            _after_reply(lambda: subprocess.run(["systemctl", final]), delay=1.5)
            return self._json({"ok": True, "rebooting": True, "poweroff": poweroff,
                               "plan": plan, "lines": lines})
        return self._json({"error": "bad action"}, 400)

    def _update_action(self, body):
        """Pull the latest code from GitHub and restart onto it."""
        if not body.get("confirm"):
            return self._json({"error": "confirmation required"}, 400)
        lines = []
        try:
            ver = updater.apply(log=lines.append, force=bool(body.get("force")))
        except Exception as e:
            return self._json({"error": str(e), "lines": lines}, 400)
        # restart the service so the new code runs (reply first; the page reconnects)
        _after_reply(webservice.restart, delay=1.0)
        return self._json({"ok": True, "version": ver, "restarting": True, "lines": lines})

    @staticmethod
    def _cookie(value, max_age):
        return f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"

    def _login(self, body):
        if not auth.check_password(body.get("password", "")):
            time.sleep(1)                      # slow down guessing
            return self._json({"error": "wrong password"}, 401)
        sid = auth.new_session()
        return self._json({"ok": True, "expires_in": auth.SESSION_TTL},
                          headers=[("Set-Cookie", self._cookie(sid, auth.SESSION_TTL))])

    def _password(self, body):
        if not auth.check_password(body.get("current", "")):
            time.sleep(1)
            return self._json({"error": "current password is wrong"}, 403)
        auth.set_password(body.get("new", ""), keep=self._sid())
        return self._json({"ok": True})

    # --- data ---
    def _status(self):
        try:
            maybe_autobuild(self.robot)      # first run: build the backup store once
        except Exception:
            pass
        info = self.robot.info()
        out = {
            "platform": info.platform, "sn": info.sn, "edition": info.edition,
            "region": info.region, "hardware": info.hardware,
            "bluetooth": info.bluetooth, "firmware": info.firmware,
            "firmware_mod": info.firmware_mod, "secure_boot": info.secure_boot,
            "identity_source": info.identity_source,
            "identity_error": info.extra.get("identity_error"),
            "loader_magic": info.extra.get("loader_magic"),
            "system": system_info(),
            "guardian": supervisor.status(),
            "webservice": webservice.status(),
            "port": self.server.fwtools_port,
            "version": config.version(),
            "job": None,
        }
        try:
            out["secdev"] = secdev_summary(self.robot)
        except Exception as e:
            out["secdev"] = {"on": None, "verdict": f"status error: {e}", "checks": []}
        cur = _JOBS.get(_CURRENT["id"])
        # the backup-store build/re-image has its own poller on the Backup tab;
        # don't let the secdev poller adopt it here
        if cur and not cur.done and not cur.name.startswith(("Build", "Re-image", "Main MCU")):
            out["job"] = cur.id
        out["backup_building"] = bool(cur and not cur.done and cur.name.startswith(("Build", "Re-image", "Main MCU")))
        return out

    # --- secondary development on/off (background job) ---
    def _secdev_toggle(self, body):
        robot = self.robot
        if body.get("action") == "on":
            if not body.get("confirm"):
                return self._json({"error": "confirmation required"}, 400)
            name = "Turn on secondary development"
            # offer_air=False: the Air modules are a separate, explicit web action
            # so a one-click enable never silently pulls ~70 MB.
            fn = lambda job: _then_mark("on", secondary_dev.enable(
                robot, assume_yes=True, log=job.log, offer_air=False))
        elif body.get("action") == "off":
            if not body.get("confirm"):
                return self._json({"error": "confirmation required"}, 400)
            name = "Turn off secondary development"
            fn = lambda job: _then_mark("off", secondary_dev.disable(robot, log=job.log))
        else:
            return self._json({"error": "bad action"}, 400)
        job = start_job(name, fn)
        if not job:
            return self._json({"error": "another operation is already running"}, 409)
        return self._json({"job": job.id})

    def _air_modules_install(self, body):
        """Install the Pro-only modules onto a Go2 Air (background job)."""
        if not body.get("confirm"):
            return self._json({"error": "confirmation required"}, 400)
        if not air_modules.is_air(self.robot):
            return self._json({"error": "not a Go2 Air"}, 400)
        robot = self.robot
        fn = lambda job: _then_mark("on", secondary_dev.install_air_modules(
            robot, assume_yes=True, log=job.log))
        job = start_job("Install Pro modules (Air)", fn)
        if not job:
            return self._json({"error": "another operation is already running"}, 409)
        return self._json({"job": job.id})

    def _service_action(self, body):
        return self._json(services.action(body.get("name"), body.get("action"), self.robot.root))

    # --- wifi AP / STA via the network_manager (bgatt) scripts ---
    def _network(self, body):
        root = self.robot.root
        action = body.get("action")
        if action == "ap":
            return self._json(network.set_ap(body.get("ssid", ""), body.get("password", ""),
                                             body.get("band", "2.4"), body.get("channel", "0"), root))
        if action == "sta":
            return self._json(network.set_sta(body.get("ssid", ""), body.get("password", ""), root))
        if action == "stop":
            return self._json(network.stop(root))
        if action == "status":
            return self._json(network.status(root))
        return self._json({"error": "bad action (expected ap|sta|stop|status)"}, 400)

    # --- guardian / web service switches (quick, synchronous) ---
    def _guardian(self, body):
        on = body.get("on")
        if not isinstance(on, bool):
            return self._json({"error": "expected {on: true|false}"}, 400)
        lines = []
        (supervisor.install if on else supervisor.uninstall)(log=lines.append)
        prefs.set("guardian", on)
        return self._json({"lines": lines, "status": supervisor.status()})

    def _webservice(self, body):
        """On = installed + started at boot. The switch also moves this page
        onto the service (or off it) without dropping the port."""
        on = body.get("on")
        if not isinstance(on, bool):
            return self._json({"error": "expected {on: true|false}"}, 400)
        lines = []
        httpd = self.server
        prefs.set("webservice", on)
        if on:
            st = webservice.install(boot=True, repo=REPO, port=httpd.fwtools_port,
                                    log=lines.append, start=False)
            port = int(st.get("port") or 0)
            if self.service_mode:
                # we ARE the service: restarting it kills us, so reply first
                lines.append("  service restarting with the new unit; the page reconnects by itself")
                _after_reply(webservice.restart)
            elif port == httpd.fwtools_port:
                # the service needs our port: hand it over, then exit
                lines.append("  this page moves to the service; it reconnects by itself (log in again)")
                _after_reply(lambda: (_stop(httpd), webservice.restart()))
            else:
                webservice.restart()
                lines.append(f"  service started on port {port}")
        elif self.service_mode:
            lines.append("  removing the service; this page goes offline now."
                         " Start it again on the robot with ./start.sh")
            _after_reply(webservice.uninstall)
        else:
            webservice.uninstall(log=lines.append)
        return self._json({"lines": lines, "status": webservice.status()})


def apply_defaults(port, service_mode, log=print):
    """Guardian and the web UI service are ON unless the user switched them off.
    Returns True if this process should hand over to the (newly installed) service."""
    p = prefs.load()
    if "guardian" not in p:
        if not supervisor.status()["installed"]:
            log("Installing the guardian (on by default; switch it off under Settings).")
            supervisor.install(log=log)
        prefs.set("guardian", True)
    if "webservice" not in p:
        prefs.set("webservice", True)
        if not service_mode and not webservice.status()["installed"]:
            log("Installing the web UI service (on by default; switch it off under Settings).")
            webservice.install(boot=True, repo=REPO, port=port, log=log, start=False)
            return True
    return False


# ---- address discovery + banner ----------------------------------------------
def _lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.123.1", 9))          # no packet sent; picks the egress IP
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:  # every interface (eth + wlan), not just the one routing to 192.168.123.x
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "up"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            ip = line.split()[3].split("/")[0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except (OSError, IndexError, subprocess.SubprocessError):
        pass
    return ips or ["<robot-ip>"]


def print_banner(port, service=False):
    bar = "═" * 60
    print("\n" + bar)
    print("  unitree_firmware_tools — web UI")
    print(bar)
    print("  Open one of these on a device on the same network:\n")
    for ip in _lan_ips():
        print(f"    http://{ip}:{port}/")
    if auth.is_default():
        print(f"\n  password: {auth.DEFAULT_PASSWORD}  (change it under Settings)")
    if not service:
        print("  Stop with Ctrl-C.")
    print(bar + "\n", flush=True)


def main():
    ap = argparse.ArgumentParser(description="unitree_firmware_tools web UI")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int,       # default: the port the installed service uses
                    default=int(os.environ.get("FWTOOLS_PORT") or webservice.read_env().get("port") or 8080))
    ap.add_argument("--platform", help="force platform (R1/G1/Go2) instead of auto-detect")
    ap.add_argument("--root", default="", help="sandbox root prefix (testing)")
    ap.add_argument("--service", action="store_true", help="running under systemd (banner only)")
    ap.add_argument("--reset-password", action="store_true",
                    help=f"set the web password back to '{auth.DEFAULT_PASSWORD}' and exit")
    args = ap.parse_args()

    if args.reset_password:
        auth.reset_password()
        print(f"Web UI password reset to '{auth.DEFAULT_PASSWORD}'.")
        return

    Handler.robot = platforms.detect(root=args.root, force=args.platform)
    Handler.service_mode = args.service
    print(f"Detected platform: {Handler.robot.platform}")

    if not args.service:
        ws = webservice.status()
        if ws.get("installed") and str(ws.get("port")) == str(args.port):
            # the service owns this port: (re)start it so it runs the code on disk now
            webservice.restart()
            print("The web UI runs as the unitree-fwtools-web service (restarted with this version):")
            print_banner(args.port, service=True)
            return
    if apply_defaults(args.port, args.service):
        webservice.restart()
        print("The web UI now runs as the unitree-fwtools-web service and starts at boot:")
        print_banner(int(webservice.read_env().get("port") or args.port), service=True)
        return

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.fwtools_port = args.port
    print_banner(args.port, service=args.service)
    try:
        httpd.serve_forever()
        print("Handed over to the unitree-fwtools-web service "
              "(systemctl status unitree-fwtools-web).")
    except KeyboardInterrupt:
        print("\nBye.")

if __name__ == "__main__":
    main()
