"""
services.py — list / start / stop the robot's services through mscli.

master_service is the supervisor mscli talks to; while it is down mscli answers
with stale data or not at all, so list() reports `fresh` and the UI says so.
"""
import json
import re
import subprocess

MSCLI = "/unitree/sbin/mscli"
VERSION_JSON = "/unitree/robot/pkg/version/version.json"
_STATES = {0: "running", 1: "stopped", -1: "error"}


def _run(args, timeout):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "mscli timed out"
    except OSError as e:
        return 127, str(e)


def _module_versions(root=""):
    try:
        with open(root + VERSION_JSON) as f:
            return json.load(f).get("Module") or {}
    except (OSError, ValueError):
        return {}


def list_services(root=""):
    master_up = subprocess.run(["pgrep", "-x", "master_service"],
                               capture_output=True).returncode == 0
    rc, out = _run([root + MSCLI, "listservice"], timeout=8)
    ok = rc == 0 and "name:" in out
    versions = _module_versions(root)
    svcs = []
    for line in out.splitlines():
        if "name:" not in line:
            continue
        d = dict(re.findall(r"(\w+):\s*([^,]+?)\s*(?:,|$)", line))
        name = (d.get("name") or "").strip()
        if not name:
            continue
        try:
            st = int(d.get("status", "99"))
        except ValueError:
            st = 99
        svcs.append({"name": name, "status": st, "state": _STATES.get(st, "unknown"),
                     "enabled": d.get("enable", "0").strip() == "1",
                     "version": versions.get(name, "")})
    err = None
    if not master_up:
        err = "master_service is not running, service states are stale"
    elif not ok:
        err = "mscli: " + (out.strip()[-160:] or "no response")
    return {"services": svcs, "master_up": master_up, "fresh": bool(master_up and ok),
            "error": err}


def action(name, act, root=""):
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("bad service name")
    cmd = {"start": "startservice", "stop": "stopservice", "restart": "restartservice"}.get(act)
    if not cmd:
        raise ValueError("bad action")
    rc, out = _run([root + MSCLI, cmd, name], timeout=30)
    # mscli's exit code is the resulting service status (0 running, 1 stopped),
    # not success/failure, so judge by whether we reached the state we wanted.
    m = re.search(r"status:\s*(-?\d+)", out)
    st = int(m.group(1)) if m else None
    want = 1 if act == "stop" else 0
    ok = (st == want) if st is not None else rc == want
    return {"ok": ok, "out": out.strip(), "status": st}
