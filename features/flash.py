"""
flash.py — reflash the root filesystem (p7) from an image staged on p8.

The running root partition cannot be written directly (it is mounted rw), so the
write is deferred to shutdown, after systemd has stopped userspace. We drop a
busybox "shutdown" handler into a tmpfs at /run/initramfs and reboot; systemd
pivots into that ramfs (the real root now unmounted), the handler decompresses
the staged image from p8 and dd's it onto p7, then reboots into the new system.
This is systemd's documented shutdown-pivot: nothing writes p7 while mounted.

Staging is non-destructive to p8's filesystem: the new rootfs is a compressed
.img.gz FILE on p8 ("flash-rootfs.img.gz"), kept alongside the factory files.
The large factory image ("linux-rootfs.img") may be removed to make room, on
request; everything else on p8 is left untouched.

DANGER: a real flash overwrites p7. If interrupted, the board must be recovered
over USB (Rockchip maskrom) from the p8 backup. The flash defaults to a DRY RUN
(decompress + measure, no write to p7); a real write needs an explicit flag.

Partitions are found by GPT name (rootfs = p7 target, userdata = p8 staging),
never by number, matching the rest of the tool.
"""
import gzip
import os
import shutil
import stat
import subprocess

from features import partitions

TARGET_LABEL = "rootfs"            # the partition this flashes (mounted at /)
STAGE_LABEL = "userdata"           # the partition the image is staged on
STAGE_MP = "/run/flash-stage"      # where p8 is mounted to read/write the image
RAMFS = "/run/initramfs"           # systemd pivots here on shutdown if ./shutdown exists
STAGE_IMG = "flash-rootfs.img.gz"  # our staged image on p8 (kept beside factory files)
FACTORY_IMG = "linux-rootfs.img"   # factory rootfs image on p8 (removable for room)
BK_SUBDIR = "backup_partitions"    # p8 folder the backup store lives in (see bkstore.py)
P7_BK_FILE = "p7_rootfs.img.gz"    # the p7 image the "backup" pivot writes into it
EXT4_MAGIC = 0xEF53
ROOTFS_LABEL = "linuxroot"      # the Go2 p7 rootfs ext4 volume label
CHUNK = 1 << 20

# Set True by bkstore while it has exclusive use of p8 (imaging it unmounted);
# _ensure_stage refuses to mount during that window so nobody reads a half-built
# filesystem out from under the imager.
_P8_BUSY = False


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _parts():
    return partitions._partitions()


def _mountpoint_of(dev):
    """Where `dev` is mounted, matched by device number so a kernel alias such
    as /dev/root (which stands in for the real root partition) is recognised."""
    want = None
    try:
        st = os.stat(dev)
        if stat.S_ISBLK(st.st_mode):
            want = st.st_rdev
    except OSError:
        pass
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) < 2:
                    continue
                if p[0] == dev:
                    return p[1]
                if want is not None:
                    try:        # the mounted device may be a kernel alias (/dev/root);
                        s2 = os.stat(p[0])      # match by block-device number,
                        if stat.S_ISBLK(s2.st_mode) and s2.st_rdev == want:
                            return p[1]
                    except OSError:
                        pass
                    try:        # or by the device backing the mountpoint itself
                        if os.stat(p[1]).st_dev == want:
                            return p[1]
                    except OSError:
                        pass
    except OSError:
        pass
    return None


# ---- target (p7) + staging (p8) discovery -----------------------------------
def target():
    """The partition this would flash (rootfs / p7)."""
    p = _parts().get(TARGET_LABEL)
    if not p:
        return {"found": False, "error": f'no partition named "{TARGET_LABEL}"'}
    return {"found": True, "dev": p["dev"], "part_size": p["size"],
            "mounted_at": _mountpoint_of(p["dev"]), "error": None}


def _stage_dev():
    p = _parts().get(STAGE_LABEL)
    return p["dev"] if p else None


def _stage_mounted_at():
    dev = _stage_dev()
    return _mountpoint_of(dev) if dev else None


def _ensure_stage(rw=False):
    """Mount p8 at STAGE_MP (if not already mounted somewhere) and return its
    mountpoint. Leaves an existing mount in place."""
    if _P8_BUSY:
        raise IOError("a partition backup is in progress; try again shortly")
    dev = _stage_dev()
    if not dev:
        raise ValueError(f'no partition named "{STAGE_LABEL}" on this device')
    mp = _mountpoint_of(dev)
    if mp:
        if rw:                     # make sure it is writable
            _run(["mount", "-o", "remount,rw", mp])
        return mp
    os.makedirs(STAGE_MP, exist_ok=True)
    opt = "rw" if rw else "ro"
    r = _run(["mount", "-t", "ext4", "-o", opt, dev, STAGE_MP])
    if r.returncode != 0:
        raise IOError(f"could not mount {dev}: {r.stderr.strip()}")
    return STAGE_MP


def _unmount_stage_if_ours():
    """Unmount p8 only if we mounted it at STAGE_MP."""
    if _mountpoint_of(_stage_dev()) == STAGE_MP:
        _run(["umount", STAGE_MP])


def _umount_p8_fully():
    """Unmount p8 from wherever it is mounted, so it can be imaged cleanly.
    Tries a lazy unmount as a fallback. Used by bkstore before imaging p8."""
    dev = _stage_dev()
    if not dev:
        return
    for _ in range(4):
        mp = _mountpoint_of(dev)
        if not mp:
            return
        if _run(["umount", mp]).returncode != 0:
            _run(["umount", "-l", mp])


def _fs_free(mp):
    try:
        st = os.statvfs(mp)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return 0, 0


def stage_info():
    """Staging view: p8 device, free space, and what's on it."""
    dev = _stage_dev()
    out = {"dev": dev, "mounted_at": None, "fs_free": 0, "fs_total": 0,
           "staged": None, "factory": None, "error": None}
    if not dev:
        out["error"] = f'no partition named "{STAGE_LABEL}" on this device'
        return out
    try:
        mp = _ensure_stage(rw=False)
    except (ValueError, IOError) as e:
        out["error"] = str(e)
        return out
    try:
        out["mounted_at"] = _mountpoint_of(dev)
        out["fs_free"], out["fs_total"] = _fs_free(mp)
        for key, name in (("staged", STAGE_IMG), ("factory", FACTORY_IMG)):
            path = os.path.join(mp, name)
            if os.path.isfile(path):
                out[key] = {"name": name, "bytes": os.path.getsize(path)}
    finally:
        _unmount_stage_if_ours()
    return out


# ---- verification -----------------------------------------------------------
def _ext4_super_from_stream(fobj):
    """Parse the ext4 superblock from the start of a raw fs stream.
    Returns {magic_ok, block_size, blocks_count, fs_bytes, label} or None."""
    head = fobj.read(2048)
    if len(head) < 2048:
        return None
    magic = int.from_bytes(head[1024 + 0x38:1024 + 0x38 + 2], "little")
    if magic != EXT4_MAGIC:
        return {"magic_ok": False}
    blocks_lo = int.from_bytes(head[1024 + 0x4:1024 + 0x8], "little")
    blocks_hi = int.from_bytes(head[1024 + 0x150:1024 + 0x154], "little")
    log_bs = int.from_bytes(head[1024 + 0x18:1024 + 0x1c], "little")
    block_size = 1024 << log_bs
    blocks = blocks_lo | (blocks_hi << 32)
    # s_volume_name[16] at superblock offset 0x78
    label = head[1024 + 0x78:1024 + 0x88].split(b"\x00")[0].decode("latin1", "replace")
    return {"magic_ok": True, "block_size": block_size, "blocks_count": blocks,
            "fs_bytes": blocks * block_size, "label": label}


def _open_image(path):
    return gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")


def verify(image_name=STAGE_IMG, integrity=False, require_rootfs=True):
    """Check the staged image: it is a gzip we can read, the decompressed stream
    is an ext4 filesystem whose volume label is the rootfs label, and it fits
    inside p7. `integrity=True` also does a full gunzip -t (slow). `require_rootfs`
    False skips the label guard (explicit override). Returns {ok, ...details}."""
    tgt = target()
    out = {"ok": False, "image": image_name, "ext4": False, "fs_bytes": 0,
           "target_bytes": tgt.get("part_size", 0), "fits": False,
           "gzip": None, "label": None, "label_ok": None, "label_mismatch": False,
           "integrity": None, "error": None}
    if not tgt["found"]:
        out["error"] = tgt["error"]
        return out
    try:
        mp = _ensure_stage(rw=False)
    except (ValueError, IOError) as e:
        out["error"] = str(e)
        return out
    try:
        path = os.path.join(mp, image_name)
        if not os.path.isfile(path):
            out["error"] = f"{image_name} is not staged on p8"
            return out
        out["gzip"] = path.endswith(".gz")
        if out["gzip"]:                        # reject a mislabeled / non-gzip .gz early
            with open(path, "rb") as rawf:
                if rawf.read(2) != b"\x1f\x8b":
                    out["error"] = "file is named .gz but is not gzip-compressed"
                    return out
        try:
            with _open_image(path) as f:
                sb = _ext4_super_from_stream(f)
        except OSError as e:
            out["error"] = f"cannot read {image_name}: {e}"
            return out
        if not sb:
            out["error"] = "image too short to contain a filesystem"
            return out
        if not sb.get("magic_ok"):
            out["error"] = "decompressed image is not ext4 (bad superblock magic)"
            return out
        out.update(ext4=True, fs_bytes=sb["fs_bytes"], block_size=sb["block_size"])
        out["label"] = sb.get("label")
        out["fits"] = sb["fs_bytes"] <= out["target_bytes"]
        if not out["fits"]:
            out["error"] = (f"filesystem is {sb['fs_bytes']} B but p7 is only "
                            f"{out['target_bytes']} B")
            return out
        # the deciding "is this really the rootfs" check: ext4 volume label
        out["label_ok"] = (out["label"] == ROOTFS_LABEL)
        if require_rootfs and not out["label_ok"]:
            out["label_mismatch"] = True
            out["error"] = (f"volume label is '{out['label']}', expected "
                            f"'{ROOTFS_LABEL}' — this does not look like a Go2 rootfs")
            return out
        if integrity and path.endswith(".gz"):
            r = _run(["gunzip", "-t", path])
            out["integrity"] = (r.returncode == 0)
            if r.returncode != 0:
                out["error"] = f"gzip integrity check failed: {r.stderr.strip()}"
                return out
        out["ok"] = True
        return out
    finally:
        _unmount_stage_if_ours()


def delete_factory(log=None):
    """Remove p8's factory linux-rootfs.img to free room for the staged image."""
    mp = _ensure_stage(rw=True)
    try:
        path = os.path.join(mp, FACTORY_IMG)
        if not os.path.isfile(path):
            if log:
                log(f"{FACTORY_IMG} not present; nothing to delete")
            return False
        n = os.path.getsize(path)
        os.remove(path)
        _run(["sync"])
        if log:
            log(f"removed {FACTORY_IMG} ({n} bytes); p8 free is now "
                f"{_fs_free(mp)[0]} bytes")
        return True
    finally:
        _unmount_stage_if_ours()


# ---- the shutdown-pivot flasher ---------------------------------------------
def _ldd_libs(binary):
    """Shared libraries (and ELF interpreter) a dynamic binary needs."""
    libs = set()
    for line in _run(["ldd", binary]).stdout.splitlines():
        line = line.strip()
        if "=>" in line:
            path = line.split("=>", 1)[1].strip().split(" ")[0]
        else:
            path = line.split(" ")[0]        # e.g. the ld-linux interpreter
        if path.startswith("/") and os.path.exists(path):
            libs.add(path)
    return libs


def _compressor():
    """A parallel gzip (pigz) to use in the pivot, or None. pigz uses every CPU
    core — ~4x faster than single-threaded gzip on the 8-core RK3588S for the
    same .gz output. Prefer one installed on the robot, else the aarch64 build
    shipped in the repo (bin/pigz-aarch64)."""
    p = shutil.which("pigz")
    if p:
        return p
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "bin", "pigz-aarch64")
    return local if os.path.exists(local) else None


def _ramfs_bins():
    """The binaries the shutdown handler needs + their shared libraries.
    busybox covers mount/dd/sync/reboot/mv/nc, but its built-in gunzip
    mis-handles images whose uncompressed size is a multiple of 4 GiB (gzip
    ISIZE wraps to 0), so (de)compression uses the real GNU gzip. pigz (if
    present) does the heavy backup compression across all cores. e2image reads
    p7 (used blocks only) for the "backup" mode.
    Returns (busybox, gzip, pigz_or_None, e2image, [libs])."""
    bb = shutil.which("busybox") or "/usr/bin/busybox"
    gz = shutil.which("gzip") or "/bin/gzip"
    e2 = shutil.which("e2image") or "/usr/sbin/e2image"
    pigz = _compressor()
    libs = _ldd_libs(bb) | _ldd_libs(gz) | _ldd_libs(e2)
    if pigz:
        libs |= _ldd_libs(pigz)
    return bb, gz, pigz, e2, sorted(libs)


def _primary_net():
    """(iface, cidr) of a wired LAN IPv4 to bring up for a DEBUG pivot, eth*
    first. Only used when a backup is armed with debug=True."""
    best = None
    for line in _run(["ip", "-o", "-4", "addr", "show", "scope", "global"]).stdout.splitlines():
        p = line.split()
        if len(p) >= 4 and p[1] != "lo":
            ifc, cidr = p[1], p[3]
            if ifc.startswith("eth"):
                return ifc, cidr
            best = best or (ifc, cidr)
    return best or (None, None)


def _shutdown_script(mode, target_dev, stage_dev, image, cores=4, use_pigz=False,
                     debug=False, net_if="None", net_cidr="None", grace=600):
    """The handler that runs as PID1 in the shutdown ramfs, with the real root
    unmounted. p8 is mounted at /stage for the log and (real/dry) the staged
    image or (backup) the destination folder. A "backup" images p7 with pigz
    (all cores) into the p8 store for the running web server to hand out after
    the reboot — no networking. The :status LED is set to a fast blink so the
    pivot is visibly alive, dump/flash timings are appended to a metrics CSV on
    p8, and progress is logged. debug=True also brings up the NIC + a shell on
    :2323 and holds the box in the pivot afterwards, for live debugging; normal
    backups never touch the network."""
    comp = f"/bin/pigz -p{cores}" if use_pigz else "/bin/gzip"
    decomp = "/bin/pigz -dc" if use_pigz else "/bin/gzip -dc"
    complabel = f"pigz-p{cores}" if use_pigz else "gzip"
    dbg = "1" if debug else "0"
    dbgtag = ", DEBUG" if debug else ""
    return f"""#!/bin/busybox sh
# flash/backup handler: runs as PID1 in the shutdown ramfs (real root unmounted).
BB=/bin/busybox
$BB mkdir -p /proc /sys /dev /oldroot /stage
$BB mount -t proc proc /proc 2>/dev/null
$BB mount -t sysfs sysfs /sys 2>/dev/null
$BB mount -t devtmpfs devtmpfs /dev 2>/dev/null
# fast-blink the status LED so it is visibly alive while root is unmounted
LED=/sys/class/leds/:status
if [ -d "$LED" ]; then
  $BB printf timer > "$LED/trigger" 2>/dev/null
  $BB printf 150 > "$LED/delay_on" 2>/dev/null
  $BB printf 150 > "$LED/delay_off" 2>/dev/null
fi
# release the old root so the target partition (p7) is not mounted
$BB umount /oldroot 2>/dev/null || $BB umount -l /oldroot 2>/dev/null
# mount staging (p8) rw, for the image/store and a persistent log
LOG=/dev/null
if $BB mount -t ext4 {stage_dev} /stage 2>/dev/null; then LOG=/stage/flash.log; fi
log() {{ $BB printf '[flash] %s\n' "$1" > /dev/kmsg 2>/dev/null; $BB printf '[flash] %s\n' "$1" >> "$LOG" 2>/dev/null; }}
# append-only timing history for analysis (iso,epoch,op,secs,bytes,note)
METRICS=/stage/flash-metrics.csv
[ -f "$METRICS" ] || $BB printf 'iso,epoch,op,secs,bytes,note\n' > "$METRICS" 2>/dev/null
metric() {{ $BB printf '%s,%s,%s,%s,%s,%s\n' "$($BB date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" "$($BB date +%s 2>/dev/null)" "$1" "$2" "$3" "$4" >> "$METRICS" 2>/dev/null; }}
log "=== handler start (mode={mode}{dbgtag}) ==="
TARGET={target_dev}; E2=/bin/e2image; IMG=/stage/{image}
BKDIR=/stage/{BK_SUBDIR}; OUT=$BKDIR/{P7_BK_FILE}
IF={net_if}; CIDR={net_cidr}
if [ "{dbg}" = "1" ] && [ "$IF" != "None" ]; then
  log "debug: bringing up $IF $CIDR + shell on :2323 (connect: nc <ip> 2323)"
  $BB ip link set "$IF" up 2>>"$LOG"; $BB ip addr add "$CIDR" dev "$IF" 2>>"$LOG"; $BB sleep 2
  $BB nc -ll -p 2323 -e /bin/busybox sh 2>>"$LOG" &
fi
# bring every CPU online: the shutdown may offline all but cpu0, which would pin
# pigz to one core (~4x slower); re-online so it spreads across all cores
for c in /sys/devices/system/cpu/cpu[0-9]*/online; do $BB printf 1 > "$c" 2>/dev/null; done
# force max CPU frequency: the pivot has no load-based scaling, so ondemand would
# leave the cores low-clocked and pigz would crawl even on all 8 cores
for g in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do $BB printf performance > "$g" 2>/dev/null; done
log "cpus online $($BB grep -c '^processor' /proc/cpuinfo 2>/dev/null); freq a55=$($BB cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq 2>/dev/null) a76=$($BB cat /sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq 2>/dev/null)"
case "{mode}" in
  backup)
    $BB mkdir -p "$BKDIR"
    $BB rm -f "$BKDIR/.p7err" "$OUT.part"
    log "backup: e2image -ar $TARGET | {comp} -> $OUT (p7 unmounted)"
    ( while [ ! -f "$OUT" ]; do [ -f "$OUT.part" ] && log "progress: $($BB wc -c < "$OUT.part" 2>/dev/null) B"; $BB sleep 5; done ) &
    MON=$!
    T0=$($BB date +%s 2>/dev/null)
    ( $E2 -ar "$TARGET" - 2>>"$LOG" || $BB touch "$BKDIR/.p7err" ) | {comp} > "$OUT.part" 2>>"$LOG"
    grc=$?; $BB sync
    T1=$($BB date +%s 2>/dev/null); DT=$(($T1 - $T0))
    $BB kill $MON 2>/dev/null
    if [ $grc -eq 0 ] && [ ! -f "$BKDIR/.p7err" ]; then
      $BB mv "$OUT.part" "$OUT"; SZ=$($BB wc -c < "$OUT" 2>/dev/null); $BB sync
      $BB printf 'secs=%s\nbytes=%s\n' "$DT" "$SZ" > "$BKDIR/.p7dump" 2>/dev/null
      metric backup "$DT" "$SZ" "{complabel}"
      log "backup: done ($SZ B in $DT s) -> $OUT"
    else
      metric backup "$DT" 0 "fail-rc$grc"
      log "ERROR: p7 backup failed (rc=$grc, $DT s); left $OUT.part"; $BB rm -f "$BKDIR/.p7err"
    fi
    ;;
  real)
    if [ ! -f "$IMG" ]; then log "ERROR: $IMG missing"; else
      IMGSZ=$($BB wc -c < "$IMG" 2>/dev/null)
      log "writing $IMG -> $TARGET ({decomp} | dd bs=4M)"
      T0=$($BB date +%s 2>/dev/null)
      {decomp} "$IMG" 2>>"$LOG" | $BB dd of="$TARGET" bs=4M 2>>"$LOG"; rc=$?; $BB sync
      T1=$($BB date +%s 2>/dev/null); DT=$(($T1 - $T0))
      if [ $rc -eq 0 ]; then metric flash "$DT" "$IMGSZ" "ok-{complabel}"; log "write complete ($IMGSZ B in $DT s); sync done";
      else metric flash "$DT" "$IMGSZ" "fail-rc$rc"; log "ERROR: dd rc=$rc ($DT s); $TARGET may be incomplete -- recover via USB maskrom"; fi
    fi
    ;;
  *)
    if [ ! -f "$IMG" ]; then log "ERROR: $IMG missing"; else
      IMGSZ=$($BB wc -c < "$IMG" 2>/dev/null)
      log "DRY RUN: not writing $TARGET; decompressing $IMG to /dev/null"
      T0=$($BB date +%s 2>/dev/null)
      {decomp} "$IMG" 2>>"$LOG" | $BB dd of=/dev/null bs=4M 2>>"$LOG"
      T1=$($BB date +%s 2>/dev/null); DT=$(($T1 - $T0))
      metric dry "$DT" "$IMGSZ" "{complabel}"
      log "DRY RUN: decompress done ($IMGSZ B in $DT s)"
    fi
    ;;
esac
$BB sync
if [ "{dbg}" = "1" ]; then log "debug: holding in pivot {grace}s (nc :2323 up); 'reboot -f' to leave early"; $BB sleep {grace}; fi
log "=== handler done; rebooting ==="
$BB umount /stage 2>/dev/null
$BB sync
$BB reboot -f 2>/dev/null
$BB echo b > /proc/sysrq-trigger 2>/dev/null
while : ; do $BB sleep 5; done
"""

def armed():
    """Is a flash staged for the next reboot? {armed, mode, image}."""
    mode_file = os.path.join(RAMFS, "flash.mode")
    sd = os.path.join(RAMFS, "shutdown")
    if not (os.path.isfile(sd) and os.path.isfile(mode_file)):
        return {"armed": False, "mode": None, "image": None}
    try:
        mode, image = open(mode_file).read().strip().split("\n", 1)
    except (OSError, ValueError):
        mode, image = "unknown", STAGE_IMG
    return {"armed": True, "mode": mode, "image": image}


def disarm(log=None):
    """Remove the shutdown handler so the next reboot is a normal one."""
    if os.path.ismount(RAMFS):
        _run(["umount", "-l", RAMFS])
    if os.path.isdir(RAMFS):
        shutil.rmtree(RAMFS, ignore_errors=True)
    if log:
        log("disarmed: /run/initramfs removed; next reboot is normal")


def arm(mode="dry", image_name=STAGE_IMG, log=None, debug=False, require_rootfs=True):
    """Build the shutdown-pivot ramfs so the NEXT reboot runs the handler.
    mode: "dry" (decompress + measure, no write), "real" (write p7 from the
    staged image), "backup" (image p7 with pigz into the p8 backup store — no
    network). debug=True (backup only) also brings a NIC + shell up in the pivot
    and holds it there, for live debugging. Does NOT reboot; the caller triggers
    it. Returns the plan dict."""
    if mode not in ("dry", "real", "backup"):
        raise ValueError("mode must be dry|real|backup")
    tgt = target()
    if not tgt["found"]:
        raise ValueError(tgt["error"])
    if mode in ("dry", "real"):                # these consume a staged image
        v = verify(image_name, require_rootfs=require_rootfs)
        if not v["ok"]:
            raise ValueError(v["error"] or "image failed verification")
    stage_dev = _stage_dev()
    bb, gz, pigz, e2, libs = _ramfs_bins()
    cores = os.cpu_count() or 4
    net_if, net_cidr = (_primary_net() if debug else (None, None))
    if log:
        log(f"building {RAMFS} (busybox+gzip+e2image"
            f"{'+pigz' if pigz else ''} + {len(libs)} libs)"
            + (f"; DEBUG net {net_if or '-'} {net_cidr or '-'}" if debug else ""))

    disarm()                                   # clean any previous arming
    # pivot_root requires the new root to be a mount point -> tmpfs
    os.makedirs(RAMFS, exist_ok=True)
    r = _run(["mount", "-t", "tmpfs", "tmpfs", RAMFS])
    if r.returncode != 0:
        raise IOError(f"could not mount tmpfs at {RAMFS}: {r.stderr.strip()}")
    for d in ("bin", "proc", "sys", "dev", "oldroot", "stage"):
        os.makedirs(os.path.join(RAMFS, d), exist_ok=True)
    shutil.copy2(bb, os.path.join(RAMFS, "bin", "busybox"))
    shutil.copy2(gz, os.path.join(RAMFS, "bin", "gzip"))   # GNU gzip (reliable >4 GiB)
    shutil.copy2(e2, os.path.join(RAMFS, "bin", "e2image"))
    if pigz:
        shutil.copy2(pigz, os.path.join(RAMFS, "bin", "pigz"))  # parallel gzip
    for src in libs:                           # keep each lib at its real path
        dst = RAMFS + src
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    script = _shutdown_script(mode, tgt["dev"], stage_dev, image_name,
                              cores=cores, use_pigz=bool(pigz), debug=debug,
                              net_if=net_if or "None", net_cidr=net_cidr or "None")
    sd = os.path.join(RAMFS, "shutdown")
    with open(sd, "w") as f:
        f.write(script)
    os.chmod(sd, 0o755)
    with open(os.path.join(RAMFS, "flash.mode"), "w") as f:
        f.write(f"{mode}\n{image_name}")
    _run(["sync"])
    plan = {"mode": mode, "image": image_name, "target": tgt["dev"],
            "stage": stage_dev, "target_bytes": tgt["part_size"],
            "compressor": "pigz" if pigz else "gzip", "cores": cores,
            "debug": bool(debug)}
    if mode == "backup":
        plan["dest"] = f"p8:/{BK_SUBDIR}/{P7_BK_FILE}"
    if mode in ("dry", "real"):
        plan["fs_bytes"] = v["fs_bytes"]
    if log:
        log(f"armed ({mode}): next reboot runs the handler for {tgt['dev']}")
    return plan


def read_log():
    """The flash handler's log from the last (attempted) flash, kept on p8."""
    try:
        mp = _ensure_stage(rw=False)
    except (ValueError, IOError) as e:
        return {"log": "", "error": str(e)}
    try:
        path = os.path.join(mp, "flash.log")
        if not os.path.isfile(path):
            return {"log": "", "error": None}
        with open(path, errors="replace") as f:
            return {"log": f.read()[-8000:], "error": None}
    finally:
        _unmount_stage_if_ours()


def read_metrics():
    """The append-only timing history the pivot writes on every p7 backup/flash
    (p8:/flash-metrics.csv). Returns {csv, rows, error}; rows parsed newest-first."""
    try:
        mp = _ensure_stage(rw=False)
    except (ValueError, IOError) as e:
        return {"csv": "", "rows": [], "error": str(e)}
    try:
        path = os.path.join(mp, "flash-metrics.csv")
        if not os.path.isfile(path):
            return {"csv": "", "rows": [], "error": None}
        with open(path, errors="replace") as f:
            text = f.read()
        rows = []
        for line in text.strip().splitlines()[1:]:      # skip header
            p = line.split(",")
            if len(p) >= 6:
                rows.append({"iso": p[0], "epoch": p[1], "op": p[2],
                             "secs": p[3], "bytes": p[4], "note": p[5]})
        rows.reverse()
        return {"csv": text[-16000:], "rows": rows, "error": None}
    finally:
        _unmount_stage_if_ours()


def status():
    """Everything the Flash UI needs."""
    return {"target": target(), "stage": stage_info(), "armed": armed(),
            "stage_image": STAGE_IMG, "factory_image": FACTORY_IMG}
