"""
partitions.py — read-only dumps of the flash partitions, for backup.

Each area is read-only and found by its GPT name (never by number); a name that
is not on this device is reported as missing. Two kinds:

  raw     a straight byte copy of the area (dd-equivalent):
            spl       Rockchip loader area in front of the first partition
                      (GPT + idbloader/SPL) — read from the raw eMMC
            uboot     U-Boot                         (crucial)
            misc      boot-control block (BCB)
            uni       identity / secure blob         (crucial)
            boot      kernel + device tree           (crucial)
            recovery  recovery image
            backup    vendor backup area
          spl/uboot/uni/boot are flagged `crucial`: the minimum set needed to
          recover a board whose flash was damaged.

  ext4gz  a filesystem image, not a raw dd — for the big, mostly-empty ext4
          partitions. Imaged with `e2image -ar` (used blocks only; free space
          and the partition tail become zero holes) piped through gzip, so only
          the real data is downloaded. gunzip the result to a sparse raw ext4
          image you can mount or flash. The partition is never written/resized.
            userdata  Go2 user data (/dev/mmcblk0p8)
"""
import hashlib
import os
import subprocess
import time
import zipfile

SYS_BLOCK = "/sys/class/block"
CHUNK = 1 << 20
SPL_MAX = 16 << 20        # the loader area is 8 MiB on RK3588 boards; never read past 16 MiB

USERDATA_LABEL = "userdata"   # the Go2's big ext4 partition (/dev/mmcblk0p8)

# name, label (None = loader area), desc, crucial, method ("raw" | "ext4gz")
AREAS = (
    ("spl",      None,       "loader area (GPT + idbloader/SPL)", True,  "raw"),
    ("uboot",    "uboot",    "U-Boot FIT (U-Boot + TF-A)",        True,  "raw"),
    ("misc",     "misc",     "boot-control block (BCB)",          False, "raw"),
    ("uni",      "uni",      "identity / secure blob",            True,  "raw"),
    ("boot",     "boot",     "kernel + device tree",              True,  "raw"),
    ("recovery", "recovery", "recovery OS (kernel + initramfs)",  False, "raw"),
    ("backup",   "backup",   "vendor reserved (empty on Go2)",    False, "raw"),
    ("userdata", "userdata", "user data filesystem (ext4)",       False, "ext4gz"),
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
    """What can be dumped here, in one list:
    [{name, label, desc, crucial, method, found, dev, offset, size, error,
      # ext4gz only: fstype, mounted, fs_size, fs_used, fs_free, clean}]."""
    parts = _partitions()
    uboot = parts.get("uboot")
    out = []
    for name, label, desc, crucial, method in AREAS:
        a = {"name": name, "label": label, "desc": desc, "crucial": crucial,
             "method": method, "found": False, "dev": None, "offset": 0,
             "size": 0, "error": None}
        if method == "ext4gz":
            a.update(_ext4_area(label))
        elif label:
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
    socket). raw areas go in as <name>.img; ext4gz areas as <name>.img.gz
    (e2image | gzip). A manifest with sizes and md5s is added last."""
    lines = list(header_lines) + [f"created   {time.strftime('%Y-%m-%d %H:%M:%S %z')}", ""]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for a in areas():
            if a["name"] not in names:
                continue
            if not a["found"]:
                lines.append(f"{a['name']:9s} MISSING  {a['error']}")
                continue
            if a["method"] == "ext4gz":
                if a["mounted"]:
                    lines.append(f"{a['name']:9s} SKIPPED  mounted at {a['mounted']}")
                    continue
                info = zipfile.ZipInfo(f"{a['name']}.img.gz", time.localtime()[:6])
                info.compress_type = zipfile.ZIP_STORED      # already gzip-compressed
                with z.open(info, "w", force_zip64=True) as w:
                    n, md5 = _e2image_gz(a["dev"], w)
                lines.append(f"{a['name']:9s} {n:>12d} B gz  md5 {md5}  "
                             f"from {a['dev']} (ext4 image of {a['fs_size']} B fs)")
            else:
                info = zipfile.ZipInfo(f"{a['name']}.img", time.localtime()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                with z.open(info, "w", force_zip64=True) as w:
                    md5 = stream(a, w)
                lines.append(f"{a['name']:9s} {a['size']:>12d} B  md5 {md5}  from {a['dev']}")
        z.writestr("manifest.txt", "\n".join(lines) + "\n")


# --- ext4 areas: a filesystem image, not a raw dd ----------------------------
# A big ext4 partition (Go2 /dev/mmcblk0p8 "userdata") is mostly empty, so a raw
# dd of the whole thing is wasteful. Instead `e2image -ar` opens the device
# READ-ONLY and writes only the in-use blocks (free space and the partition tail
# past the filesystem become zero holes), piped through gzip so the holes
# collapse. gunzip the result to a sparse raw ext4 image you can mount or flash.
# The live partition is never written to and never resized.

def _mounts():
    """{device: mountpoint} from /proc/mounts (first mount wins)."""
    out = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2:
                    out.setdefault(p[0], p[1])
    except OSError:
        pass
    return out


def _ext4_super(dev):
    """Read-only ext4 superblock facts via `dumpe2fs -h`; {} if not ext4."""
    try:
        p = subprocess.run(["dumpe2fs", "-h", dev],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if p.returncode != 0:
        return {}
    d = {}
    for line in p.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    try:
        bs, bc, fb = (int(d["Block size"]), int(d["Block count"]), int(d["Free blocks"]))
    except (KeyError, ValueError):
        return {}
    if not bs or not bc:
        return {}
    return {"block_size": bs, "block_count": bc, "free_blocks": fb,
            "fs_size": bc * bs, "fs_free": fb * bs, "fs_used": (bc - fb) * bs,
            "clean": d.get("Filesystem state", "") == "clean"}


def _ext4_area(label):
    """Fields to merge into an ext4gz area: the partition, its ext4 filesystem,
    and whether it can be imaged right now (read-only). `size` is the apparent
    image size (the filesystem size)."""
    out = {"found": False, "dev": None, "size": 0, "error": None, "fstype": None,
           "mounted": None, "part_size": 0, "fs_size": 0, "fs_used": 0,
           "fs_free": 0, "clean": None}
    p = _partitions().get(label)
    if not p:
        out["error"] = f'no partition named "{label}" on this device'
        return out
    out.update(dev=p["dev"], part_size=p["size"], mounted=_mounts().get(p["dev"]))
    sb = _ext4_super(p["dev"])
    if not sb:
        out["error"] = f'{p["dev"]} is not an ext4 filesystem (or is unreadable)'
        return out
    out.update(found=True, fstype="ext4", size=sb["fs_size"], fs_size=sb["fs_size"],
               fs_used=sb["fs_used"], fs_free=sb["fs_free"], clean=sb["clean"])
    return out


def _e2image_gz(dev, out):
    """Pipe `e2image -ar <dev> - | gzip` to the writable `out` (device opened
    read-only; nothing written to it). Returns (gz_bytes, md5-of-gz-stream)."""
    e2 = subprocess.Popen(["e2image", "-ar", dev, "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    gz = subprocess.Popen(["gzip", "-c"], stdin=e2.stdout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    e2.stdout.close()        # gzip now owns the read end; let e2image see SIGPIPE
    h = hashlib.md5()
    total = 0
    try:
        while True:
            chunk = gz.stdout.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            out.write(chunk)
            total += len(chunk)
    finally:
        gz.stdout.close()
        e2_err = (e2.stderr.read() or b"").decode(errors="replace"); e2.stderr.close()
        gz_err = (gz.stderr.read() or b"").decode(errors="replace"); gz.stderr.close()
        erc, grc = e2.wait(), gz.wait()
    # e2image prints its version banner to stderr even on success; only rc matters.
    if erc != 0:
        raise IOError(f"e2image failed (rc {erc}): {e2_err.strip() or 'unknown error'}")
    if grc != 0:
        raise IOError(f"gzip failed (rc {grc}): {gz_err.strip() or 'unknown error'}")
    return total, h.hexdigest()


def stream_ext4_gz(name, out, log=None):
    """Stream an ext4gz area (by name) as a gzipped e2image to `out` (a socket).
    Returns {dev, fs_size, gz_bytes, md5}."""
    a = area(name)
    if a["method"] != "ext4gz":
        raise ValueError(f"{name} is not an ext4 image area")
    if a["mounted"]:
        raise ValueError(f'{a["dev"]} is mounted at {a["mounted"]}; '
                         "unmount it before imaging")
    total, md5 = _e2image_gz(a["dev"], out)
    if log:
        log(f"  {name} image: {total} B gzip  md5 {md5}  from {a['dev']}")
    return {"dev": a["dev"], "fs_size": a["fs_size"], "gz_bytes": total, "md5": md5}
