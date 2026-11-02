"""
mcu_update.py — flash the Go2 main MCU (motor/power controller) firmware.

A main-MCU update is the two files an OTA's basic_module ships:
    /unitree/robot/tool/boot/AppPublic.bin   the MCU firmware image
    /unitree/robot/tool/boot/McuBoot         the aarch64 flasher
and the exact command sequence from that module.json:
    stopservice sport_mode / advanced_sport / basic_service   (CmdPreList)
    cd /unitree/robot/tool/boot; ./McuBoot start_Mcu_Upgrade  (CmdProcList, must be 0)
    rm McuBoot AppPublic.bin                                   (CmdPostList)
McuBoot talks to /dev/GBUNICH7 and prints "mcu boot is ok" on success.

The firmware versions live in a Yandex folder (go2/firmware/main_mcu): one
main_mcu_<ver>.zip per version plus an index.json catalog. This module lists the
catalog (live from the folder, falling back to the copy shipped in
assets/manifests/Go2.main_mcu.json) and flashes the version the user picks.
"""
import json
import os
import subprocess

import config
from core import yandex

CATALOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "assets", "manifests", "Go2.main_mcu.json")
BOOT_DIR = "/unitree/robot/tool/boot"
DEFAULT_DEVICE = "/dev/GBUNICH7"
# The MCU exposes NO firmware-version query over the wire (the custom app/
# bootloader protocol has no "get version" command, and McuBoot flashes without
# checking any version — verified by RE). So the only version of record is the
# installed OTA module version, tracked here:
VERSION_JSON = "/unitree/robot/pkg/version/version.json"   # Module.basic_module
# The MCU files live in a subfolder of the shared Go2 Yandex folder, so we reuse
# that existing public link (config.MODEL_FOLDERS["Go2"]) + this subpath.
DEFAULT_SUBPATH = "/firmware/main_mcu"
_PLACEHOLDER = "PASTE"            # the un-filled folder_link marker


def is_go2(robot):
    try:
        return (robot.platform or "").lower() == "go2"
    except Exception:
        return False


# ---- catalog ---------------------------------------------------------------
def _shipped():
    try:
        with open(CATALOG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"versions": [], "folder_link": "", "device": DEFAULT_DEVICE}


def folder_link():
    """Public link of the Yandex folder the MCU files live in. Reuses the shared
    Go2 folder link; "" only if nothing is configured at all."""
    link = (config.MODEL_FOLDERS.get("Go2_main_mcu")          # optional override
            or _shipped().get("folder_link")
            or config.MODEL_FOLDERS.get("Go2") or "").strip()
    return "" if (not link or _PLACEHOLDER in link) else link


def subpath():
    """Folder (within the public link) that holds index.json + the zips."""
    return _shipped().get("subpath", DEFAULT_SUBPATH).rstrip("/")


def catalog(live=False):
    """{versions, folder_link, subpath, device, source}. With live=True, try the
    folder's index.json over the network (slow, ~seconds); otherwise return the
    shipped copy instantly. The page loads with live=False; a refresh asks live."""
    local = _shipped()
    link, sub = folder_link(), subpath()
    out = {"versions": local.get("versions", []),
           "folder_link": link, "subpath": sub,
           "device": local.get("device", DEFAULT_DEVICE),
           "source": "local"}
    if live and link:
        try:
            got = yandex.fetch_json(link, path=f"{sub}/index.json", timeout=6)
            if isinstance(got, dict) and got.get("versions"):
                out["versions"] = got["versions"]
                out["device"] = got.get("device", out["device"])
                out["source"] = "yandex"
        except Exception:
            pass          # offline / not uploaded yet -> keep the shipped list
    return out


def _entry(cat, version):
    for v in cat.get("versions", []):
        if v.get("version") == version:
            return v
    return None


# ---- current version on the robot ------------------------------------------
def _root(robot):
    return getattr(robot, "root", "") or ""


def installed_version(robot):
    """MCU firmware version the module system records = version.json's
    `basic_module` (the OTA module that carries AppPublic.bin). None if unknown.
    This is set at install time, not read live from the chip."""
    try:
        with open(_root(robot) + VERSION_JSON) as f:
            return (json.load(f).get("Module") or {}).get("basic_module")
    except Exception:
        return None


# ---- status (for the web panel) --------------------------------------------
def status(robot, live=False):
    cat = catalog(live=live)
    dev = cat.get("device", DEFAULT_DEVICE)
    return {
        "is_go2": is_go2(robot),
        "device": dev,
        "device_present": os.path.exists(dev),
        "folder_ok": bool(cat.get("folder_link")),
        "source": cat.get("source"),
        "installed": installed_version(robot),
        "versions": [{"version": v.get("version"), "label": v.get("label") or v.get("version"),
                      "default": bool(v.get("default"))}
                     for v in cat.get("versions", [])],
    }


# ---- flash -----------------------------------------------------------------
def _sh(cmd, log, ok_codes):
    log(f"$ {cmd}")
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    for line in (p.stdout + p.stderr).splitlines():
        log("  " + line)
    if p.returncode not in ok_codes:
        raise RuntimeError(f"command failed (exit {p.returncode}): {cmd}")
    return p.returncode, (p.stdout + p.stderr)


def update(version, log=print, robot_info=None, device=None):
    """Download + flash the chosen MCU firmware version. Faithful to the OTA's
    basic_module Install (stop services -> McuBoot start_Mcu_Upgrade -> cleanup).
    Raises on failure; returns a dict on success."""
    cat = catalog(live=True)          # authoritative: can flash a folder-only version
    entry = _entry(cat, version)
    if not entry:
        raise ValueError(f"unknown MCU version {version!r}")
    link = cat.get("folder_link")
    if not link:
        raise ValueError("no Yandex folder link set for the main-MCU catalog "
                         "(fill folder_link in assets/manifests/Go2.main_mcu.json).")
    sub = cat.get("subpath", DEFAULT_SUBPATH).rstrip("/")
    dev = device or cat.get("device", DEFAULT_DEVICE)
    if not os.path.exists(dev):
        raise RuntimeError(f"MCU serial device {dev} is not present — is this a Go2?")

    # 1. download + extract the package (from <folder>/<subpath>/<zip>)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    log(f"Downloading {entry['zip']} …")
    zpath = yandex.download(link, config.DOWNLOAD_DIR, path=f"{sub}/{entry['zip']}",
                            expect_md5=entry.get("zip_md5"), filename=entry["zip"])
    import tempfile
    pkg = tempfile.mkdtemp(prefix="fwtools_mcu_")
    try:
        yandex.extract_zip(zpath, pkg)
        with open(os.path.join(pkg, "manifest.json")) as f:
            man = json.load(f)

        # 2. place the files (size-verified), make McuBoot executable
        os.makedirs(BOOT_DIR, exist_ok=True)
        for dest, meta in man["files"].items():
            src = os.path.join(pkg, meta["src"])
            data = open(src, "rb").read()
            if len(data) != meta.get("size", len(data)):
                raise RuntimeError(f"{os.path.basename(dest)} size "
                                   f"{len(data)} != expected {meta['size']}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as out:
                out.write(data)
            if meta.get("exec"):
                os.chmod(dest, 0o755)
            log(f"  placed {dest} ({len(data)} B)")

        inst = man["install"]
        # 3. CmdPreList — stop the services that touch the bus (tolerant)
        for cmd in inst.get("pre", []):
            _sh(cmd, log, inst.get("pre_ok", [0]))
        # 4. CmdProcList — the actual flash (must be 0 AND print the marker)
        proc = inst["proc"]
        if device:                                # honour a device override
            proc = proc.replace("./McuBoot", f"./McuBoot -D {device}")
        _, out = _sh(proc, log, inst.get("proc_ok", [0]))
        marker = man.get("success_marker", "mcu boot is ok")
        if marker and marker not in out:
            raise RuntimeError(f"flash did not report success ({marker!r} not seen) "
                               "— MCU may be unchanged; power-cycle and re-check.")
        log(f"MCU reported: {marker}")
        # 5. CmdPostList — cleanup (never fatal)
        for cmd in inst.get("post", []):
            try:
                _sh(cmd, log, list(range(256)))
            except Exception:
                pass
        log("Main MCU update complete. Power-cycle the robot to boot the new MCU firmware.")
        return {"ok": True, "version": version}
    finally:
        import shutil
        shutil.rmtree(pkg, ignore_errors=True)
