"""
bkstore.py — an on-device backup store on p8 (userdata).

The idea: instead of streaming partition images over the network from the
shutdown pivot, we keep a folder of ready-made images ON the robot, inside the
big p8 filesystem, and let the normal web server (:8080) hand them out. No
networking in the pivot, no WiFi, no progress-bar-with-no-server problem.

Layout, under  p8:/backup_partitions/ :
    spl.img.gz uboot.img.gz misc.img.gz uni.img.gz boot.img.gz
    recovery.img.gz backup.img.gz        raw dumps of the loader + p1..p6
    p8_original.img.gz                    ext4 image of p8 as first seen
    p7_rootfs.img.gz                      ext4 image of p7 (written by the pivot)
    manifest.json  .done                  metadata + "built" marker

build() captures everything except p7 (which is the running root and needs the
shutdown pivot, see flash.py). Because p8 cannot be imaged cleanly while it is
mounted — and we also need it mounted to *write* the store into it — the order
matters:

    1. image p8 "original" while UNMOUNTED, to a temp file on the roomy rootfs
    2. mount p8 rw, grow its filesystem to fill the partition (resize2fs),
       so there is room for the images (the factory fs is only ~half the part)
    3. move the p8 image in, then dump the loader + p1..p6 raw-gzipped
    4. write manifest.json + .done

p7 is added later by the flash "backup" pivot, which drops its image straight
into the same folder. listing() then shows it for download like the rest.

Everything here is read-only to the partitions it images; the only writes are
to the p8 *filesystem* (the store folder) and the one-time resize2fs grow.
"""
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time

from features import flash, partitions

BKDIR = flash.BK_SUBDIR            # "backup_partitions" (shared with the pivot)
P7_FILE = flash.P7_BK_FILE         # "p7_rootfs.img.gz"  (written by the pivot)
P8_FILE = "p8_original.img.gz"
DONE = ".done"
MANIFEST = "manifest.json"
TMPDIR = "/var/tmp/fwtools-bkstore"   # on the rootfs (roomy); holds the p8 image briefly
CHUNK = 1 << 20

# For the UI's "please wait" countdown on a p7 backup: the pivot is offline the
# whole time, so the estimate is reboot overhead + the dump itself. The dump
# time is dominated by gzip (single-threaded), so the best predictor is simply
# how long it took last time; before that, scale p7's used bytes by a
# conservative rate. OVERHEAD covers the two reboots + the service coming back.
P7_OVERHEAD_SECS = 140                # two reboots + boot + services, measured ~138s
P7_DEFAULT_RATE = 28 << 20            # bytes/s of p7 used-data through e2image|pigz
                                      # in the pivot (measured ~30 MB/s; e2image +
                                      # eMMC-write bound, not CPU). Only the first-
                                      # ever estimate uses this; later ones use the
                                      # last real dump time from .p7dump.

_LOCK = threading.Lock()           # serialises build / destructive p8 work


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# ---- the store's canonical contents -----------------------------------------
def _store_name(area_name):
    """File name a raw/ext4 area is stored under."""
    return P8_FILE if area_name == "userdata" else f"{area_name}.img.gz"


def _spec():
    """Ordered [(filename, meta)] for everything build() produces (not p7).
    meta = {area, desc, crucial, method}."""
    out = []
    for name, _label, desc, crucial, method in partitions.AREAS:
        out.append((_store_name(name),
                    {"area": name, "desc": desc, "crucial": crucial, "method": method}))
    return out


# ---- md5 / gzip helpers -----------------------------------------------------
def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def _raw_gz(area, path):
    """Dump a raw area (loader or a small partition) gzipped to `path`.
    Returns (gz_bytes, md5_of_gz_file)."""
    with open(path, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as g:
        left = area["size"]
        with open(area["dev"], "rb") as src:       # read-only
            src.seek(area["offset"])
            while left > 0:
                chunk = src.read(min(CHUNK, left))
                if not chunk:
                    raise IOError(f"short read on {area['dev']}")
                g.write(chunk)
                left -= len(chunk)
    return os.path.getsize(path), _md5_file(path)


# ---- build ------------------------------------------------------------------
def is_built():
    """True if a completed store exists on p8 (cheap; mounts p8 ro)."""
    dev = flash._stage_dev()
    if not dev or flash._P8_BUSY:
        return False
    try:
        mp = flash._ensure_stage(rw=False)
    except (ValueError, IOError):
        return False
    return os.path.isfile(os.path.join(mp, BKDIR, DONE))


def _read_manifest(store):
    try:
        with open(os.path.join(store, MANIFEST)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build(log=print, robot_info=None):
    """Capture the loader + p1..p6 + p8 into p8:/backup_partitions. Grows the p8
    filesystem first so there is room. Returns False (never asks for a reboot).
    Raises on a fatal error. Safe to re-run (overwrites the store)."""
    if not _LOCK.acquire(blocking=False):
        raise IOError("a backup-store operation is already running")
    flash._P8_BUSY = True
    try:
        return _build_locked(log, robot_info)
    finally:
        flash._P8_BUSY = False
        _LOCK.release()


def _build_locked(log, robot_info):
    dev = flash._stage_dev()
    if not dev:
        raise ValueError('no "userdata" (p8) partition on this device')
    part_bytes = partitions._partitions()["userdata"]["size"]
    log(f"backup store: p8 = {dev}  ({part_bytes // (1 << 20)} MiB partition)")

    # 1. image p8 ORIGINAL while unmounted -> temp on the rootfs
    flash._umount_p8_fully()
    sb0 = partitions._ext4_super(dev)
    if not sb0:
        raise IOError(f"{dev} is not a readable ext4 filesystem")
    os.makedirs(TMPDIR, exist_ok=True)
    tmp8 = os.path.join(TMPDIR, P8_FILE)
    free_root = shutil.disk_usage("/").free
    log(f"imaging p8 original ({sb0['fs_used'] // (1 << 20)} MiB used of "
        f"{sb0['fs_size'] // (1 << 20)} MiB fs) → temp on rootfs "
        f"({free_root // (1 << 20)} MiB free there)")
    with open(tmp8, "wb") as f:
        n8, md5_8 = partitions._e2image_gz(dev, f)
    log(f"  p8 original: {n8 // (1 << 20)} MiB gz")

    files = {}
    try:
        # 2. mount p8 rw and grow the filesystem to fill the partition.
        # Mount directly (not _ensure_stage) because _P8_BUSY is set, which is
        # exactly what blocks everyone *else* from touching p8 right now.
        mp = flash.STAGE_MP
        os.makedirs(mp, exist_ok=True)
        r = _run(["mount", "-t", "ext4", "-o", "rw", dev, mp])
        if r.returncode != 0 and not flash._mountpoint_of(dev):
            raise IOError(f"could not mount {dev} rw: {r.stderr.strip()}")
        mp = flash._mountpoint_of(dev) or mp
        grew = None
        if sb0["fs_size"] < part_bytes:
            log(f"growing p8 filesystem {sb0['fs_size'] // (1 << 20)} → "
                f"{part_bytes // (1 << 20)} MiB (resize2fs, one-time)")
            r = _run(["resize2fs", dev])
            if r.returncode != 0:
                log(f"  resize2fs warning (continuing): {r.stderr.strip()}")
            else:
                grew = [sb0["fs_size"], part_bytes]
                log("  grown")
        else:
            log("p8 filesystem already fills the partition; no grow needed")

        # 3a. store folder + move the p8 image in
        store = os.path.join(mp, BKDIR)
        os.makedirs(store, exist_ok=True)
        # clear a previous (completed) marker while we rebuild
        try:
            os.remove(os.path.join(store, DONE))
        except OSError:
            pass
        shutil.move(tmp8, os.path.join(store, P8_FILE))
        files[P8_FILE] = {"area": "userdata", "desc": "user data filesystem (ext4)",
                          "crucial": False, "method": "ext4gz", "bytes": n8,
                          "md5": md5_8, "source": dev}

        # 3b. the loader + p1..p6, raw-gzipped
        for a in partitions.areas():
            if a["method"] != "raw":
                continue
            fn = _store_name(a["name"])
            if not a["found"]:
                log(f"  skip {a['name']}: {a['error']}")
                continue
            log(f"imaging {a['name']} ({a['size'] // (1 << 20) or 1} MiB) from {a['dev']}")
            sz, md5 = _raw_gz(a, os.path.join(store, fn))
            files[fn] = {"area": a["name"], "desc": a["desc"], "crucial": a["crucial"],
                         "method": "raw", "bytes": sz, "md5": md5,
                         "source": a["dev"], "part_bytes": a["size"]}

        # 4. manifest + done marker
        man = {"created": time.strftime("%Y-%m-%d %H:%M:%S %z"),
               "robot": robot_info or {}, "grew_p8": grew, "files": files}
        with open(os.path.join(store, MANIFEST), "w") as f:
            json.dump(man, f, indent=2)
        open(os.path.join(store, DONE), "w").close()
        _run(["sync"])
        free, total = flash._fs_free(mp)
        log(f"backup store complete: {len(files)} images in p8:/{BKDIR} "
            f"({free // (1 << 20)} MiB free of {total // (1 << 20)} MiB)")
        return False
    finally:
        flash._umount_p8_fully()         # leave p8 unmounted; listing() remounts ro
        try:
            if os.path.isfile(tmp8):
                os.remove(tmp8)
        except OSError:
            pass


def build_one(area_name, log=print, robot_info=None):
    """Re-image a SINGLE area (loader / p1..p6 / p8) into an already-built store,
    overwriting just that one image and updating the manifest entry. p7 is not
    handled here (it needs the shutdown pivot — see flash.py / backup_p7)."""
    if not _LOCK.acquire(blocking=False):
        raise IOError("a backup-store operation is already running")
    flash._P8_BUSY = True
    try:
        return _build_one_locked(area_name, log, robot_info)
    finally:
        flash._P8_BUSY = False
        _LOCK.release()


def _build_one_locked(area_name, log, robot_info):
    dev = flash._stage_dev()
    if not dev:
        raise ValueError('no "userdata" (p8) partition on this device')
    fn = _store_name(area_name)
    if fn not in dict(_spec()):
        raise ValueError(f"cannot re-image {area_name!r} here (p7 uses the reboot backup)")
    a = partitions.area(area_name)          # raises ValueError if missing/not found

    # p8 "userdata" must be imaged while UNMOUNTED (like build step 1) to a temp
    # on the roomy rootfs, then moved into the store once p8 is mounted rw.
    tmp = None
    if area_name == "userdata":
        flash._umount_p8_fully()
        sb0 = partitions._ext4_super(dev)
        if not sb0:
            raise IOError(f"{dev} is not a readable ext4 filesystem")
        os.makedirs(TMPDIR, exist_ok=True)
        tmp = os.path.join(TMPDIR, P8_FILE)
        log(f"imaging p8 original ({sb0['fs_used'] // (1 << 20)} MiB used) → temp on rootfs")
        with open(tmp, "wb") as f:
            n8, md5_8 = partitions._e2image_gz(dev, f)

    mp = flash.STAGE_MP
    os.makedirs(mp, exist_ok=True)
    r = _run(["mount", "-t", "ext4", "-o", "rw", dev, mp])
    if r.returncode != 0 and not flash._mountpoint_of(dev):
        raise IOError(f"could not mount {dev} rw: {r.stderr.strip()}")
    mp = flash._mountpoint_of(dev) or mp
    try:
        store = os.path.join(mp, BKDIR)
        if not os.path.isfile(os.path.join(store, DONE)):
            raise IOError("no backup set on p8 yet — build the full set first")
        dest = os.path.join(store, fn)
        if area_name == "userdata":
            shutil.move(tmp, dest)
            tmp = None
            meta = {"area": "userdata", "desc": a["desc"], "crucial": False,
                    "method": "ext4gz", "bytes": n8, "md5": md5_8, "source": dev}
        else:
            log(f"imaging {area_name} ({a['size'] // (1 << 20) or 1} MiB) from {a['dev']}")
            sz, md5 = _raw_gz(a, dest)
            meta = {"area": area_name, "desc": a["desc"], "crucial": a["crucial"],
                    "method": "raw", "bytes": sz, "md5": md5,
                    "source": a["dev"], "part_bytes": a["size"]}
        # update just this file's manifest entry; keep the rest + the .done marker
        man = _read_manifest(store)
        man.setdefault("files", {})[fn] = meta
        man["updated"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
        if robot_info and not man.get("robot"):
            man["robot"] = robot_info
        with open(os.path.join(store, MANIFEST), "w") as f:
            json.dump(man, f, indent=2)
        _run(["sync"])
        log(f"re-imaged {area_name}: {meta['bytes'] // (1 << 20) or 1} MiB gz")
        return False
    finally:
        flash._umount_p8_fully()            # leave p8 unmounted; listing() remounts ro
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---- listing + serving ------------------------------------------------------
def listing():
    """What the Backup tab shows. Mounts p8 ro and leaves it mounted (cheap)."""
    out = {"built": False, "busy": flash._P8_BUSY, "dir": BKDIR, "error": None,
           "created": None, "grew_p8": None, "fs_free": 0, "fs_total": 0,
           "files": [], "p7": None}
    dev = flash._stage_dev()
    if not dev:
        out["error"] = 'no "userdata" (p8) partition on this device'
        return out
    if flash._P8_BUSY:
        return out                      # a build is running; UI polls the job
    try:
        mp = flash._ensure_stage(rw=False)
    except (ValueError, IOError) as e:
        out["error"] = str(e)
        return out
    store = os.path.join(mp, BKDIR)
    man = _read_manifest(store)
    mfiles = man.get("files", {})
    out["built"] = os.path.isfile(os.path.join(store, DONE))
    out["created"] = man.get("created")
    out["grew_p8"] = man.get("grew_p8")
    out["fs_free"], out["fs_total"] = flash._fs_free(mp)

    # area -> on-disk device (e.g. mmcblk0p2), so the UI can show the location.
    # Prefer the live partition table; fall back to the manifest's recorded source.
    dev_by_area = {}
    try:
        for a in partitions.areas():
            if a.get("found") and a.get("dev"):
                dev_by_area[a["name"]] = os.path.basename(a["dev"])
    except Exception:
        pass

    def _dev(area, fn):
        d = dev_by_area.get(area) or os.path.basename(mfiles.get(fn, {}).get("source", "") or "")
        return d or None

    def entry(fn, meta):
        path = os.path.join(store, fn)
        present = os.path.isfile(path)
        return {"file": fn, "area": meta.get("area"), "desc": meta.get("desc"),
                "crucial": bool(meta.get("crucial")), "method": meta.get("method"),
                "dev": _dev(meta.get("area"), fn),
                "present": present,
                "bytes": os.path.getsize(path) if present else 0,
                "mtime": int(os.path.getmtime(path)) if present else None,
                "md5": mfiles.get(fn, {}).get("md5")}

    for fn, meta in _spec():
        out["files"].append(entry(fn, meta))
    # p7 lives in the same folder but is produced by the pivot, not build()
    p7 = entry(P7_FILE, {"area": "rootfs", "desc": "root filesystem (ext4)",
                         "crucial": True, "method": "ext4gz"})
    try:
        p7["dev"] = os.path.basename(flash.target().get("dev") or "") or None
    except Exception:
        p7["dev"] = None
    p7["armed"] = flash.armed()
    p7.update(_p7_estimate(store))
    out["p7"] = p7
    return out


def _p7_estimate(store):
    """How long a p7 backup is likely to take, for the UI countdown:
    {used_bytes, last_dump_secs, est_secs}."""
    used = None
    try:
        tgt = flash.target()
        mp = tgt.get("mounted_at") or "/"
        st = os.statvfs(mp)
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        pass
    last = None
    try:
        with open(os.path.join(store, ".p7dump")) as f:
            d = dict(l.split("=", 1) for l in f.read().splitlines() if "=" in l)
        last = int(d.get("secs"))
    except (OSError, ValueError, TypeError):
        pass
    base = last if last else (used / P7_DEFAULT_RATE if used else None)
    est = int(P7_OVERHEAD_SECS + base) if base else None
    return {"used_bytes": used, "last_dump_secs": last, "est_secs": est}


def _allowed_files():
    return {fn for fn, _ in _spec()} | {P7_FILE}


def file_info(name):
    """(abs path, size) for a store file to download, or None. Mounts p8 ro."""
    if name not in _allowed_files() or flash._P8_BUSY:
        return None
    try:
        mp = flash._ensure_stage(rw=False)
    except (ValueError, IOError):
        return None
    path = os.path.join(mp, BKDIR, name)
    if not os.path.isfile(path):
        return None
    return path, os.path.getsize(path)


def stream_file(path, out):
    """Copy a store file to the writable `out` (p8 must already be mounted)."""
    with open(path, "rb") as f:
        shutil.copyfileobj(f, out, CHUNK)


def present_files():
    """[(name, path, size)] for every store file that exists right now."""
    out = []
    dev = flash._stage_dev()
    if not dev or flash._P8_BUSY:
        return out
    try:
        mp = flash._ensure_stage(rw=False)
    except (ValueError, IOError):
        return out
    store = os.path.join(mp, BKDIR)
    for name in [fn for fn, _ in _spec()] + [P7_FILE]:
        path = os.path.join(store, name)
        if os.path.isfile(path):
            out.append((name, path, os.path.getsize(path)))
    return out
