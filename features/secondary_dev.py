"""
secondary_dev.py — enable / disable / status of secondary development.

Enable flow (R1/G1):
  1. require the guardian failsafe (auto-offer install) so a bad patch can't lock you out,
  2. resolve the package (Yandex download+verify, or a local dir/zip),
  3. preflight + confirm,
  4. stop the running stack the OTA way (mscli stopservice … + master), behind a
     motion-safety confirm, so no live service clobbers a patched file,
  5. md5-gated backup+replace+verify (core.installer),
  6. power-cycle (turn OFF then ON) to load the new binaries.
Disable = stop the stack + restore the newest backup (or factory) + power-cycle.
"""
import glob
import os
import tempfile

import config
from core import installer, secdev, selector, service_control, yandex
from . import air_modules, supervisor


def stop_stack_guarded(robot, assume_yes=False, confirm=input, log=print, crouch=False):
    """Stop the running stack the OTA way (full stop incl. master) before we
    replace any binary, with a motion-safety gate. Returns True if stopped.

    Patched files only take effect on reboot, but a live service can rewrite the
    file we just patched — so we stop first, exactly like Install.CmdPreList.
    crouch: on a Go2, lower it to the ground (OTA zero-position) before stopping.
    """
    if robot.platform not in ("R1", "G1"):
        return service_control.stop_stack(robot.platform, include_master=True,
                                          log=log, crouch_first=crouch)
    log("\n⚠  R1/G1 is a STANDING humanoid. Stopping the motion stack will drop it.")
    log("   Lay the robot down / secure it on a stand BEFORE continuing.")
    if not assume_yes:
        ans = confirm("   Type STOP to stop the stack (anything else cancels): ").strip()
        if ans != "STOP":
            log("   Stack stop cancelled — nothing changed.")
            return False
    return service_control.stop_stack(robot.platform, include_master=True, log=log)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_DIR = os.path.join(REPO, "assets", "manifests")


def _load_manifest_file(name):
    path = os.path.join(MANIFEST_DIR, name)
    return installer.load_manifest(path) if os.path.isfile(path) else None


def local_manifest(robot):
    """Load the bundled manifest that matches the robot in front of us.

    Uses the model's general manifest (selector) to pick the version by firmware
    string, or by md5 fingerprint when the reported version has no package. Falls
    back to a direct <Platform>_<firmware>.json, then to the newest bundled one.
    """
    platform = robot.platform
    sel = selector.select(robot)
    if sel:
        m = _load_manifest_file(sel["entry"]["manifest"])
        if m:
            return m
    fw = robot.firmware_version()[0]
    exact = os.path.join(MANIFEST_DIR, f"{platform}_{fw}.json")
    if os.path.isfile(exact):
        return installer.load_manifest(exact)
    cand = sorted(glob.glob(os.path.join(MANIFEST_DIR, f"{platform}_*.json")))
    return installer.load_manifest(cand[-1]) if cand else None


def status(robot, probe_lowcmd=True):
    """Version-independent verdict via observable behavior (edition + open DDS +
    plaintext lowcmd), plus an optional per-file md5 detail if a manifest is
    bundled. Returns (verdict_dict, files_report_or_None).

    verdict_dict = {enabled: True/False/None, verdict: str, signals: {...}}.
    Pass probe_lowcmd=False to skip the ~3s live rt/lowcmd DDS probe (info card).
    """
    verdict = secdev.detect(robot, probe_lowcmd=probe_lowcmd)
    files = None
    try:
        sel = selector.select(robot)
        if sel:
            how = "version match" if sel["how"] == "version" else "md5 fingerprint"
            verdict["signals"]["package"] = f"{robot.platform} {sel['entry']['firmware']} ({how})"
    except Exception:
        pass
    m = local_manifest(robot)
    if m:
        try:
            files = installer.Installer(root=robot.root).status(m)[1]
        except Exception:
            files = None
    return verdict, files


def resolve_package(robot, local=None, log=print):
    """Return (pkg_dir, manifest, cleanup_dir_or_None). Downloads if needed.

    Without --package, the package is chosen from the model's general manifest
    (by firmware version, or by md5 fingerprint if that version has no dedicated
    package) and pulled from the model's Yandex folder link.
    """
    cleanup = None
    if local:
        if os.path.isdir(local):
            pkg_dir = local
        elif local.endswith(".zip"):
            pkg_dir = tempfile.mkdtemp(prefix="fwtools_pkg_"); cleanup = pkg_dir
            yandex.extract_zip(local, pkg_dir)
        else:
            raise ValueError("--package must be a directory or a .zip")
        manifest = installer.load_manifest(os.path.join(pkg_dir, "manifest.json"))
        return pkg_dir, manifest, cleanup

    fw = robot.firmware_version()[0]
    index = selector.load_index(robot.platform)
    if not index:
        raise ValueError(
            f"no general manifest for {robot.platform} (assets/manifests/{robot.platform}.index.json). "
            "Install from a local package with --package <dir|zip>.")
    sel = selector.select(robot, index=index)
    if not sel:
        raise ValueError(
            f"no package matches {robot.platform} firmware {fw or '?'}: it is not a packaged "
            "version and its on-disk service binaries match no known vanilla/patched set. "
            "Install from a local package with --package <dir|zip>.")
    entry = sel["entry"]
    if sel["how"] == "fingerprint":
        log(f"Firmware {fw or '?'} has no dedicated package; matched {robot.platform} "
            f"{entry['firmware']} by md5 fingerprint ({sel['state'] or 'binaries'}"
            + (f"; also matches {', '.join(v for v in sel['matches'] if v != entry['firmware'])}"
               if len(sel["matches"]) > 1 else "") + ").")
    else:
        log(f"Selected {robot.platform} {entry['firmware']} package (by firmware version).")

    folder = config.folder_for(robot.platform) or index.get("folder_link")
    if not folder:
        raise ValueError(
            f"no Yandex folder link for {robot.platform} (set config.MODEL_FOLDERS). "
            "Install from a local package with --package <dir|zip>.")
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    log(f"Downloading {entry['zip']}…")
    zpath = yandex.download(folder, config.DOWNLOAD_DIR, path="/" + entry["zip"],
                            expect_md5=entry.get("zip_md5"), filename=entry["zip"])
    pkg_dir = tempfile.mkdtemp(prefix="fwtools_pkg_"); cleanup = pkg_dir
    yandex.extract_zip(zpath, pkg_dir)
    manifest = installer.load_manifest(os.path.join(pkg_dir, "manifest.json"))
    return pkg_dir, manifest, cleanup


def enable(robot, local=None, assume_yes=False, confirm=input, log=print, offer_air=True):
    """Enable secondary development. Returns True if a reboot is warranted.

    On a Go2 **Air**, once the patch is in, offers to add the Pro-only modules the
    Air lacks (ai_sport/advanced_sport/unitree_lidar_slam/chat_go/utrack/4gcm),
    which a v2 package carries alongside the patch (see features.air_modules).
    `offer_air=False` suppresses the offer (the web UI drives the Air modules as a
    separate, explicit action so a one-click enable never silently writes ~90 MB
    of modules)."""
    # 1) guardian gate (hard prerequisite on humanoids)
    if robot.platform in ("R1", "G1"):
        st = supervisor.status()
        if not st["installed"]:
            log("\nThe SSH/WiFi guardian failsafe is REQUIRED before patching R1/G1 "
                "(a bad patch can otherwise lock you out).")
            ans = "y" if assume_yes else confirm("Install the guardian now? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                supervisor.install(log=log)
                if not supervisor.status()["installed"]:
                    log("Guardian install failed — aborting."); return False
            else:
                log("Aborting: guardian is required."); return False

    pkg_dir, manifest, cleanup = resolve_package(robot, local=local, log=log)
    try:
        inst = installer.Installer(root=robot.root)
        pf = inst.preflight(pkg_dir, manifest)
        log("\nPlan:")
        for r in pf:
            log(f"  {r['name']:20s} {r['verdict']}")
        if all(r["verdict"] == installer.INSTALLED for r in pf):
            log("\nAlready fully enabled. Nothing to do."); return False
        if not assume_yes:
            ans = confirm("\nProceed with backup + install? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                log("Cancelled."); return False
        # stop the running stack (OTA way) so no live service clobbers the patch
        stop_stack_guarded(robot, assume_yes=assume_yes, confirm=confirm, log=log)
        log("")
        backup_dir, _ = inst.install(pkg_dir, manifest, log=log)
        if backup_dir:
            log(f"\nOriginals backed up to: {backup_dir}")
        log("Secondary development installed. Power-cycle the robot (turn OFF, then ON) to activate.")
        # Go2 Air: offer to add the Pro-only modules it ships without, from this
        # same package (v2 carries the firmware-matched module builds).
        if offer_air:
            _offer_air_modules(robot, pkg_dir, manifest, assume_yes=assume_yes,
                               confirm=confirm, log=log)
        return True
    finally:
        if cleanup:
            import shutil; shutil.rmtree(cleanup, ignore_errors=True)


def _offer_air_modules(robot, pkg_dir, manifest, assume_yes=False, confirm=input, log=print):
    """On a Go2 Air, offer to add the Pro-only modules this package carries."""
    if not air_modules.is_air(robot):
        return
    names = air_modules.modules_in_package(pkg_dir, manifest)
    if not names:
        return
    meta = (manifest.get("air_modules") or {})
    log("\nThis is a Go2 AIR. This package also carries the Pro-only modules it ships without:")
    log("  " + ", ".join(names))
    if meta.get("total_bytes"):
        log(f"  (~{meta['total_bytes'] / 1e6:.0f} MB on disk)")
    if not assume_yes:
        ans = confirm("Install the Pro modules now? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            log("Skipped — you can run this later from the Secondary-development menu.")
            return
    try:
        air_modules.install_from_package(robot, pkg_dir, manifest,
                                         assume_yes=True, confirm=confirm, log=log)
    except Exception as e:
        log(f"Air-module install failed: {e}")


def install_air_modules(robot, local=None, assume_yes=False, force=False,
                        confirm=input, log=print):
    """Standalone: install the Pro-only modules onto an Air from its secdev
    package (resolved/downloaded like enable). Returns reboot-warranted."""
    pkg_dir, manifest, cleanup = resolve_package(robot, local=local, log=log)
    try:
        if not air_modules.modules_in_package(pkg_dir, manifest):
            log("This firmware's package carries no Air modules (it may be a v1 package). "
                "Nothing to install.")
            return False
        return air_modules.install_from_package(
            robot, pkg_dir, manifest, assume_yes=assume_yes, force=force,
            confirm=confirm, log=log)
    finally:
        if cleanup:
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)


def list_backups(robot):
    """Newest-first list of restore points (for the picker)."""
    return installer.Installer(root=robot.root).list_backups()


def restore_backup(robot, backup=None, assume_yes=False, confirm=input, log=print):
    """Restore the ORIGINALS saved before patching. `backup` = a dict from
    list_backups() (default: newest). Fully offline. Returns reboot-warranted."""
    inst = installer.Installer(root=robot.root)
    if backup is None:
        backups = inst.list_backups()
        if not backups:
            log("No backups found. Use 'Restore factory' to install the vanilla files "
                "from the package instead."); return False
        backup = backups[0]
    log(f"Restoring backup {backup['ts']} (the files saved before patching):")
    if not assume_yes and confirm("Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        log("Cancelled."); return False
    stop_stack_guarded(robot, assume_yes=assume_yes, confirm=confirm, log=log)
    inst.restore_backup(backup, log=log)
    log("Restored. Power-cycle the robot (turn OFF, then ON) to activate.")
    return True


def restore_factory(robot, local=None, assume_yes=False, confirm=input, log=print):
    """Install the vanilla files shipped in the package's factory/ tree, IGNORING any
    local backups (needs a download or --package). Returns reboot-warranted."""
    try:
        pkg_dir, manifest, cleanup = resolve_package(robot, local=local, log=log)
    except Exception as e:
        log(f"Cannot factory-restore: {e}"); return False
    try:
        if not manifest.get("has_factory"):
            log("This package carries no factory/ originals."); return False
        log("Factory restore installs the ORIGINAL vanilla files from the package, "
            "ignoring any local backups.")
        if not assume_yes and confirm("Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
            log("Cancelled."); return False
        stop_stack_guarded(robot, assume_yes=assume_yes, confirm=confirm, log=log)
        installer.Installer(root=robot.root).restore_factory(pkg_dir, manifest, log=log)
        log("Factory-restored. Power-cycle the robot (turn OFF, then ON) to activate.")
        return True
    finally:
        if cleanup:
            import shutil; shutil.rmtree(cleanup, ignore_errors=True)


def disable(robot, local=None, log=print):
    """Turn secondary development off: restore the newest backup that holds THIS
    firmware's originals (a backup taken before an OTA holds older files), else
    the package's factory files. Returns reboot-warranted."""
    m = local_manifest(robot)
    if m:
        _, report = installer.Installer(root=robot.root).status(m)
        if report and all(r["verdict"] == installer.VANILLA for r in report):
            log("Already stock: every target file has its original md5. Nothing to do.")
            return False
    backup = usable_backup(robot, manifest=m)
    if backup:
        return restore_backup(robot, backup=backup, assume_yes=True, log=log)
    log("No backup of this firmware's originals, restoring the factory files from the package.")
    return restore_factory(robot, local=local, assume_yes=True, log=log)


def usable_backup(robot, manifest=None):
    """Newest backup whose files are this firmware's originals, or None."""
    m = manifest or local_manifest(robot)
    originals = {e["name"]: e.get("original_md5") for e in (m or {}).get("files", [])}
    for b in list_backups(robot):
        files = b["manifest"].get("files", [])
        if files and (not originals or all(originals.get(f["name"]) == f["md5"] for f in files)):
            return b
    return None


# NOTE: no software reboot. On Unitree robots a soft `reboot` only cycles the
# Linux SoC (not the MCU/motors/power rail), so the stack comes back wrong — the
# user must power-cycle (physical off/on). app._power_cycle_notice() says so.


# ---- power-cycle pending --------------------------------------------------------
# After a switch the new files only load at the next power-on. The marker lives in
# /run (tmpfs, wiped at boot) and is also dropped once master_service is running
# again with a start time after the switch, i.e. the new binaries are loaded.
PENDING_FILE = "/run/unitree-fwtools/power-cycle.json"


def mark_power_cycle(action):
    import json, time
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, "w") as f:
        json.dump({"action": action, "time": time.time()}, f)


def _master_started():
    """Epoch start time of the oldest running master_service, or None."""
    import subprocess
    r = subprocess.run(["pgrep", "-o", "-x", "master_service"], capture_output=True, text=True)
    pid = r.stdout.strip()
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])    # field 22: starttime
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime"))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def power_cycle_pending():
    """{action: "on"|"off", time} while the robot still has to be power-cycled, else None."""
    import json
    try:
        with open(PENDING_FILE) as f:
            p = json.load(f)
    except (OSError, ValueError):
        return None
    started = _master_started()
    if started is not None and started > p.get("time", 0):
        try:
            os.remove(PENDING_FILE)
        except OSError:
            pass
        return None
    return p
