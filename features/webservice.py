"""
webservice.py — install/remove the unitree-fwtools-web systemd service so the
browser UI auto-starts at boot.

Mirrors features/supervisor.py. The unit runs `webapp.py --service`; the port
lives in /etc/unitree-fwtools/web.env (referenced by the unit via
EnvironmentFile=). Login is by password (features/auth.py).
"""
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT_SRC = os.path.join(REPO, "assets", "webservice", "unitree-fwtools-web.service")
UNIT_DST = "/etc/systemd/system/unitree-fwtools-web.service"
ENV_DIR = "/etc/unitree-fwtools"
ENV_FILE = os.path.join(ENV_DIR, "web.env")
NAME = "unitree-fwtools-web"


def _run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def is_installed():
    return os.path.isfile(UNIT_DST)


def status():
    if not is_installed():
        return {"installed": False, "enabled": False, "active": False}
    en = _run(f"systemctl is-enabled {NAME} 2>/dev/null").stdout.strip()
    ac = _run(f"systemctl is-active {NAME} 2>/dev/null").stdout.strip()
    st = {"installed": True, "enabled": en == "enabled", "active": ac == "active",
          "enabled_raw": en, "active_raw": ac}
    st.update(read_env())
    return st


def read_env():
    """The port the installed service uses."""
    out = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        return {}
    return {"port": out.get("FWTOOLS_PORT", "")}


def install(boot=True, repo=REPO, port=8080, log=print, start=True):
    """Install (and optionally enable-at-boot) the web-UI service.

    boot=False installs + starts it now but does NOT enable it at boot.
    start=False leaves starting it to the caller (the web UI hands its port over).
    Keeps the port already in web.env so the address stays stable on reinstall.
    """
    if not os.path.isfile(UNIT_SRC):
        raise FileNotFoundError("web-service unit template missing from the tool")

    port = read_env().get("port") or str(port)
    os.makedirs(ENV_DIR, exist_ok=True)
    with open(ENV_FILE, "w") as f:
        f.write(f"FWTOOLS_PORT={port}\n")

    with open(UNIT_SRC) as f:
        unit = f.read().replace("__REPO__", repo)
    with open(UNIT_DST, "w") as f:
        f.write(unit)
    os.chmod(UNIT_DST, 0o644)
    log(f"  ✓ installed {NAME} unit (repo={repo}, port={port})")

    cmds = ["systemctl daemon-reload"]
    if boot:
        cmds.append(f"systemctl enable {NAME}")
    if start:
        cmds.append(f"systemctl restart {NAME}")
    for cmd in cmds:
        r = _run(cmd)
        if r.returncode != 0 and r.stderr.strip():
            log(f"  ! {cmd}: {r.stderr.strip()}")
    st = status()
    log(f"  {NAME}: enabled={st['enabled']} active={st['active']} "
        f"(auto-start at boot: {'yes' if boot else 'no'})")
    return st


def restart():
    _run(f"systemctl reset-failed {NAME}")     # clear a start-limit hit (e.g. after a redeploy)
    return _run(f"systemctl restart {NAME}")


def uninstall(log=print):
    # stop goes last: when the service uninstalls itself, stopping kills this process
    _run(f"systemctl disable {NAME}")
    try:
        os.remove(UNIT_DST)
    except FileNotFoundError:
        pass
    _run("systemctl daemon-reload")
    log(f"  ✓ {NAME} removed")
    _run(f"systemctl stop {NAME}")
    return True
