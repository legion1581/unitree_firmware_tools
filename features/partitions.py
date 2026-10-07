"""
partitions.py — read-only dumps of the boot-critical flash areas, for backup.

    spl    the Rockchip loader area (GPT + idbloader/SPL) in front of the first
           partition, read from the raw eMMC (the old go2 tool's pre-uboot.img)
    uboot  partition labelled "uboot"
    boot   partition labelled "boot"
    uni    partition labelled "uni" (the robot's identity / secure blob)

Partitions are found by their GPT name, never by number. A name that is not on
this device is reported as missing. Devices are only ever opened read-only.
"""
import hashlib
import os
import time
import zipfile

SYS_BLOCK = "/sys/class/block"
CHUNK = 1 << 20
SPL_MAX = 16 << 20        # the loader area is 8 MiB on RK3588 boards; never read past 16 MiB

AREAS = (
    ("spl", None, "loader area in front of the first partition (GPT + idbloader/SPL)"),
    ("uboot", "uboot", "U-Boot"),
    ("boot", "boot", "kernel + device tree"),
    ("uni", "uni", "identity / secure blob"),
)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _partitions():
    """{partname: {"dev", "disk", "start", "size"}} for every named partition."""
    out = {}
    try:
        names = os.listdir(SYS_BLOCK)
    except OSError:
        return out
    for n in names:
        base = os.path.join(SYS_BLOCK, n)
        if not os.path.isfile(os.path.join(base, "partition")):
            continue
        ue = dict(l.split("=", 1) for l in _read(os.path.join(base, "uevent")).splitlines() if "=" in l)
        label = ue.get("PARTNAME")
        if not label:
            continue
        disk = os.path.basename(os.path.dirname(os.path.realpath(base)))
        out[label] = {"dev": "/dev/" + n, "disk": disk,
                      "start": int(_read(os.path.join(base, "start")) or 0),
                      "size": int(_read(os.path.join(base, "size")) or 0) * 512}
    return out


def areas():
    """What can be dumped here: [{name, label, desc, found, dev, offset, size, error}]."""
    parts = _partitions()
    uboot = parts.get("uboot")
    out = []
    for name, label, desc in AREAS:
        a = {"name": name, "label": label, "desc": desc, "found": False,
             "dev": None, "offset": 0, "size": 0, "error": None}
        if label:
            p = parts.get(label)
            if p:
                a.update(found=True, dev=p["dev"], size=p["size"])
            else:
                a["error"] = f'no partition named "{label}" on this device'
        else:
            # the loader sits in front of the first partition of the disk that holds uboot
            disk = uboot["disk"] if uboot else None
            starts = [p["start"] for p in parts.values() if p["disk"] == disk]
            if not disk:
                a["error"] = 'no partition named "uboot", so the loader area can\'t be located'
            elif not starts or min(starts) * 512 > SPL_MAX:
                a["error"] = f"unexpected layout on /dev/{disk}"
            else:
                a.update(found=True, dev="/dev/" + disk, size=min(starts) * 512)
        out.append(a)
    return out


def area(name):
    a = next((x for x in areas() if x["name"] == name), None)
    if not a:
        raise ValueError(f"unknown area: {name}")
    if not a["found"]:
        raise ValueError(a["error"])
    return a


def stream(a, out):
    """Copy an area to the writable `out`; returns its md5."""
    h = hashlib.md5()
    left = a["size"]
    with open(a["dev"], "rb") as f:          # read-only
        f.seek(a["offset"])
        while left > 0:
            chunk = f.read(min(CHUNK, left))
            if not chunk:
                raise IOError(f"short read on {a['dev']}")
            h.update(chunk)
            out.write(chunk)
            left -= len(chunk)
    return h.hexdigest()


def write_zip(names, out, header_lines=()):
    """Stream a zip of the chosen areas to `out` (no temp file; works on a
    socket). A manifest with sizes and md5s is added last."""
    lines = list(header_lines) + [f"created   {time.strftime('%Y-%m-%d %H:%M:%S %z')}", ""]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for a in areas():
            if a["name"] not in names:
                continue
            if not a["found"]:
                lines.append(f"{a['name']:6s} MISSING  {a['error']}")
                continue
            info = zipfile.ZipInfo(f"{a['name']}.img", time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            with z.open(info, "w", force_zip64=True) as w:
                md5 = stream(a, w)
            lines.append(f"{a['name']:6s} {a['size']:>10d} B  md5 {md5}  from {a['dev']}")
        z.writestr("manifest.txt", "\n".join(lines) + "\n")
