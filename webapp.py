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

import platforms
from features import auth, partitions, prefs, secondary_dev, services, supervisor, webservice

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
    }


# ---- system info (Status tab) ------------------------------------------------
def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def system_info():
    up = _read("/proc/uptime").split()
    mem = dict(re.findall(r"(\w+):\s+(\d+) kB", _read("/proc/meminfo")))
    try:
        st = os.statvfs("/unitree")
        disk = {"free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}
    except OSError:
        disk = None
    return {
        "hostname": socket.gethostname(),
        "uptime": float(up[0]) if up else None,
        "load": _read("/proc/loadavg").split()[:3],
        "mem_total": int(mem.get("MemTotal", 0)) * 1024,
        "mem_avail": int(mem.get("MemAvailable", 0)) * 1024,
        "disk": disk,
        "kernel": os.uname().release,
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
    server_version = "fwtools/2.0"
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
                               "default_password": auth.is_default()})
        if not self._authed():
            return self._json({"error": "login required"}, 401)
        try:
            if u.path == "/api/status":
                return self._json(self._status())
            if u.path == "/api/secdev":
                return self._json(secdev_summary(self.robot, probe=qs.get("probe", ["0"])[0] == "1"))
            if u.path == "/api/services":
                return self._json(services.list_services(self.robot.root))
            if u.path == "/api/backup":
                return self._json({"areas": partitions.areas()})
            if u.path == "/api/backup/zip":
                names = [n for n in (qs.get("parts", [""])[0]).split(",") if n]
                return self._backup_zip(names or [a[0] for a in partitions.AREAS])
            if u.path.startswith("/api/backup/img/"):
                return self._backup_img(u.path.rsplit("/", 1)[-1])
            if u.path.startswith("/api/job/"):
                job = _JOBS.get(u.path.rsplit("/", 1)[-1])
                return self._json(job.snapshot() if job else {"error": "no such job"},
                                  200 if job else 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
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
                     "/api/services": self._service_action,
                     "/api/console": lambda b: self._json(console_exec(b.get("cmd", ""), b.get("timeout"))),
                     "/api/guardian": self._guardian,
                     "/api/webservice": self._webservice,
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

    # --- partition backup (streamed downloads) ---
    def _backup_name(self, what, ext):
        info = self.robot.info()
        parts = [info.platform, info.sn or "unknown-sn", info.firmware or "unknown-fw", what,
                 time.strftime("%Y%m%d-%H%M%S")]
        return "_".join(p.replace(" ", "-") for p in parts) + ext

    def _backup_zip(self, names):
        known = [a[0] for a in partitions.AREAS]
        bad = [n for n in names if n not in known]
        if bad:
            return self._json({"error": f"unknown area: {', '.join(bad)}"}, 400)
        found = {a["name"] for a in partitions.areas() if a["found"]}
        if not any(n in found for n in names):
            return self._json({"error": "none of the chosen partitions exist on this device"}, 404)
        info = self.robot.info()
        header = [f"robot     {info.platform} {info.edition}", f"serial    {info.sn or '-'}",
                  f"firmware  {info.firmware or '-'}"]
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{self._backup_name("partitions", ".zip")}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()                       # no length: streamed, connection closes at the end
        partitions.write_zip(names, self.wfile, header)

    def _backup_img(self, name):
        try:
            a = partitions.area(name)
        except ValueError as e:
            return self._json({"error": str(e)}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(a["size"]))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{self._backup_name(name, ".img")}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        partitions.stream(a, self.wfile)

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
            "job": None,
        }
        try:
            out["secdev"] = secdev_summary(self.robot)
        except Exception as e:
            out["secdev"] = {"on": None, "verdict": f"status error: {e}", "checks": []}
        cur = _JOBS.get(_CURRENT["id"])
        if cur and not cur.done:
            out["job"] = cur.id
        return out

    # --- secondary development on/off (background job) ---
    def _secdev_toggle(self, body):
        robot = self.robot
        if body.get("action") == "on":
            if not body.get("confirm"):
                return self._json({"error": "confirmation required"}, 400)
            name = "Turn on secondary development"
            fn = lambda job: _then_mark("on", secondary_dev.enable(robot, assume_yes=True, log=job.log))
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

    def _service_action(self, body):
        return self._json(services.action(body.get("name"), body.get("action"), self.robot.root))

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
