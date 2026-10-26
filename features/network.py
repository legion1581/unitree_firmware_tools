"""
network.py — switch the robot's WiFi between AP and STA using the robot's own
network_manager (bgatt / upper_bluetooth) scripts — the same ones its BLE
provisioning drives, so behaviour matches the stock app.

    AP  : <nm>/ap_connect/ap_connect.sh   -s <ssid> -p <pass> -b <2.4|5> -c <chan>
    STA : <nm>/wpa_connect/wpa_connect.sh -i wlan0 -s <ssid> [-p <pass>]
    stop: the same two scripts with -S

The scripts persist the choice in /unitree/etc/wifi/WIFI_MODE (AP|STA|UNSET), which
wifi_init.sh re-applies at boot, so a mode set here survives a reboot.
"""
import glob
import os
import re
import subprocess

WIFI_MODE_FILE = "/unitree/etc/wifi/WIFI_MODE"
HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
WPA_CONF = "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
IFACE = "wlan0"
_SSID_RE = re.compile(r'^[^\x00-\x1f]{1,32}$')


def _nmdir(root=""):
    """The network_manager/upper_bluetooth dir (layout varies by firmware version)."""
    for pat in ("/unitree/module/network_manager/upper_bluetooth",
                "/unitree/module/network_manager/*/upper_bluetooth"):
        for d in sorted(glob.glob(root + pat)):
            if os.path.isdir(os.path.join(d, "ap_connect")) or os.path.isdir(os.path.join(d, "wpa_connect")):
                return d
    return None


def _run(args, timeout=120):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ss" % timeout
    except OSError as e:
        return 127, str(e)


def _wlan_up(root=""):
    try:
        with open(root + "/sys/class/net/%s/carrier" % IFACE) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def _iface_ip(root=""):
    rc, out = _run(["ip", "-4", "-o", "addr", "show", IFACE], 5)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out) if rc == 0 else None
    return m.group(1) if m else ""


def _hostapd_ssid(root=""):
    # ap_connect.sh runs hostapd from its own runtime/hostapd.conf; prefer that
    # (it reflects the AP that is actually up), then fall back to the system conf.
    d = _nmdir(root)
    paths = ([os.path.join(d, "ap_connect", "runtime", "hostapd.conf")] if d else []) + [root + HOSTAPD_CONF]
    for p in paths:
        try:
            for line in open(p):
                if line.startswith("ssid="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return ""


def _wpa_ssid(root=""):
    try:
        for line in open(root + WPA_CONF):
            m = re.search(r'^\s*ssid\s*=\s*"?([^"]*)"?\s*$', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


def status(root=""):
    mode = ""
    try:
        with open(root + WIFI_MODE_FILE) as f:
            mode = f.read().strip()
    except OSError:
        pass
    return {
        "mode": mode or "UNSET",
        "iface": IFACE,
        "up": _wlan_up(root),
        "ip": _iface_ip(root),
        "ap_ssid": _hostapd_ssid(root),
        "sta_ssid": _wpa_ssid(root),
        "available": _nmdir(root) is not None,
    }


def _check_ssid(ssid):
    if not ssid or not _SSID_RE.match(ssid):
        raise ValueError("SSID must be 1-32 characters (no control characters)")


def set_ap(ssid, password, band="2.4", channel="0", root=""):
    _check_ssid(ssid)
    if not password or not (8 <= len(password) <= 63):
        raise ValueError("AP password must be 8-63 characters")
    if str(band) not in ("2.4", "5"):
        raise ValueError("band must be 2.4 or 5")
    if not re.fullmatch(r"\d{1,3}", str(channel)):
        raise ValueError("channel must be a number (0 = auto)")
    d = _nmdir(root)
    if not d:
        raise ValueError("network_manager scripts not found on this robot")
    rc, out = _run(["sudo", os.path.join(d, "ap_connect", "ap_connect.sh"),
                    "-s", ssid, "-p", password, "-b", str(band), "-c", str(channel)])
    return {"ok": rc == 0, "rc": rc, "out": out, "status": status(root)}


def set_sta(ssid, password="", root=""):
    _check_ssid(ssid)
    if password and not (8 <= len(password) <= 63):
        raise ValueError("WiFi password must be 8-63 characters (or empty for an open network)")
    d = _nmdir(root)
    if not d:
        raise ValueError("network_manager scripts not found on this robot")
    args = ["sudo", os.path.join(d, "wpa_connect", "wpa_connect.sh"), "-i", IFACE, "-s", ssid]
    if password:
        args += ["-p", password]
    rc, out = _run(args)
    return {"ok": rc == 0, "rc": rc, "out": out, "status": status(root)}


def stop(root=""):
    d = _nmdir(root)
    if not d:
        raise ValueError("network_manager scripts not found on this robot")
    outs = []
    for rel in (("ap_connect", "ap_connect.sh", "-S"),
                ("wpa_connect", "wpa_connect.sh", "-i", IFACE, "-S")):
        rc, o = _run(["sudo", os.path.join(d, rel[0], rel[1])] + list(rel[2:]))
        outs.append(o)
    return {"ok": True, "rc": 0, "out": "\n".join(x for x in outs if x), "status": status(root)}
