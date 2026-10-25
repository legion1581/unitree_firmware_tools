"""
air_modules.py — install the Pro-only modules onto a Go2 Air.

The Air ships one firmware base with six modules fused off vs Pro/Max/Edu:
ai_sport, advanced_sport, unitree_lidar_slam, chat_go, utrack, 4gcm. Once
secondary development is enabled (edition decision opened up), those modules can
simply be added — they are plain mscli-managed modules, not edition-locked.

This feature pulls a redistributable payload (the newest build of each module,
see assets/manifests/Go2.air_modules.json) from the model's Yandex folder — or a
local dir/zip — and installs them the OTA way via core.module_installer. It is
gated to AIR by default (Pro/Max/Edu already have them); `force=True` overrides.

Note: the payload mixes the newest build of each module across firmwares, so the
set was never shipped together. ai_sport in particular couples to the locomotion
stack (sport_mode/advanced_sport/motion_switcher) and its DDS IDL on the robot;
if a mode misbehaves after install, fall back to the Pro build that matches the
Air's own firmware. Install is fully backed up and reversible (remove()).
"""
import os
import tempfile

import config
from core import module_installer, yandex

# the six Pro-only modules (Air lacks these); order = core -> leaf for install
TARGET_MODULES = ["4gcm", "advanced_sport", "ai_sport",
                  "unitree_lidar_slam", "utrack", "chat_go"]

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_MANIFEST = os.path.join(REPO, "assets", "manifests", "Go2.air_modules.json")


def catalog():
    """The bundled payload description (modules + versions + sizes), or None.

    Lets the UI show what *would* be installed without downloading the zip."""
    import json
    if not os.path.isfile(ASSET_MANIFEST):
        return None
    with open(ASSET_MANIFEST) as f:
        return json.load(f)


def is_air(robot):
    try:
        return robot.info().edition == "AIR"
    except Exception:
        return False


def resolve_payload(robot, local=None, log=print):
    """Return (pkg_dir, cleanup_dir_or_None). Downloads the Yandex zip if needed."""
    cleanup = None
    if local:
        if os.path.isdir(local):
            return local, None
        if local.endswith(".zip"):
            pkg_dir = tempfile.mkdtemp(prefix="fwtools_airmods_")
            yandex.extract_zip(local, pkg_dir)
            return _payload_root(pkg_dir), pkg_dir
        raise ValueError("--air-modules must be a directory or a .zip")

    spec = config.air_modules_for(robot.platform)
    if not spec or not spec.get("folder"):
        raise ValueError(
            f"no Air-modules payload configured for {robot.platform} "
            "(set config.AIR_MODULES). Install from a local path instead.")
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    log(f"Downloading {spec['zip']}…")
    zpath = yandex.download(spec["folder"], config.DOWNLOAD_DIR, path="/" + spec["zip"],
                            expect_md5=spec.get("zip_md5"), filename=spec["zip"])
    pkg_dir = tempfile.mkdtemp(prefix="fwtools_airmods_")
    cleanup = pkg_dir
    yandex.extract_zip(zpath, pkg_dir)
    return _payload_root(pkg_dir), cleanup


def _payload_root(extract_dir):
    """A zip may extract to <dir>/<payload>/manifest.json; find the manifest."""
    if os.path.isfile(os.path.join(extract_dir, "manifest.json")):
        return extract_dir
    for name in os.listdir(extract_dir):
        sub = os.path.join(extract_dir, name)
        if os.path.isfile(os.path.join(sub, "manifest.json")):
            return sub
    return extract_dir


def status(robot):
    """Which target modules are already on the robot. Returns a report list
    (uses the payload only to know file counts — falls back to the catalog)."""
    inst = module_installer.ModuleInstaller(root=robot.root)
    rows = []
    for name in TARGET_MODULES:
        # a module is "present" if its /unitree/module/<name> dir exists
        mdir = robot.root + f"/unitree/module/{name}"
        rows.append({"name": name, "present": os.path.isdir(mdir)})
    return rows


def install(robot, local=None, assume_yes=False, force=False, confirm=input, log=print):
    """Install the Pro-only modules onto this robot. Returns True if a
    power-cycle is warranted."""
    ed = None
    try:
        ed = robot.info().edition
    except Exception:
        pass
    if ed != "AIR" and not force:
        log(f"Edition is {ed or '?'} — these modules are Air-only "
            f"(Pro/Max/Edu already ship them). Nothing to do. Use force to override.")
        return False
    if ed != "AIR" and force:
        log(f"⚠ Forcing Air-module install onto a {ed or '?'} robot.")

    pkg_dir, cleanup = resolve_payload(robot, local=local, log=log)
    try:
        inst = module_installer.ModuleInstaller(root=robot.root)
        pf = inst.preflight(pkg_dir, TARGET_MODULES)
        log("\nModules to install (newest build of each):")
        for r in pf:
            tag = "already present" if r["present"] == r["files"] else \
                  f"{r['present']}/{r['files']} files present" if r["present"] else "new"
            log(f"  {r['name']:20s} v{r['version']:12s} {r['files']:4d} files "
                f"{r['bytes']/1e6:6.1f} MB  ({tag})")
        if not assume_yes:
            ans = confirm("\nInstall these modules? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                log("Cancelled."); return False
        _, results = inst.install(pkg_dir, TARGET_MODULES, log=log)
        done = [r["name"] for r in results if r["action"] == "installed"]
        log(f"\nInstalled {len(done)} module(s): {', '.join(done)}")
        log("Power-cycle the robot (turn OFF, then ON) to load them.")
        return True
    finally:
        if cleanup:
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)


def remove(robot, local=None, assume_yes=False, confirm=input, log=print):
    """Remove the Pro-only modules (removeservice + rm) and restore any backups."""
    pkg_dir, cleanup = resolve_payload(robot, local=local, log=log)
    try:
        if not assume_yes:
            ans = confirm("Remove the Air-added Pro modules? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                log("Cancelled."); return False
        module_installer.ModuleInstaller(root=robot.root).remove(pkg_dir, TARGET_MODULES, log=log)
        log("Removed. Power-cycle the robot to finish.")
        return True
    finally:
        if cleanup:
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)
