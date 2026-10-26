"""
air_modules.py — install the Pro-only modules onto a Go2 Air.

A Go2 Air ships one firmware base with six modules fused off vs Pro/Max/Edu:
ai_sport, advanced_sport, unitree_lidar_slam, chat_go, utrack, 4gcm. Once
secondary development is enabled (the edition decision opened up) those are plain
mscli-managed modules, not edition-locked, so they can simply be added.

Each secondary-dev package (v2+) carries, next to the patch files, the build of
each missing module that a **Pro of that same firmware** runs — so the modules
match the robot's firmware instead of being a latest-of-everything mix. This
module therefore installs straight from the already-extracted secdev package
(core.module_installer); it does not download anything of its own. It is gated to
AIR by default (Pro/Max/Edu already have them); `force=True` overrides.

The per-firmware set is also described offline in
assets/manifests/Go2.air_modules.json (schema 2) so the UI can show what would be
installed without unpacking a package. Install is fully backed up and reversible.
"""
import json
import os

from core import module_installer

# the six Pro-only modules an Air lacks (for status display / ordering)
TARGET_MODULES = ["4gcm", "advanced_sport", "ai_sport",
                  "unitree_lidar_slam", "utrack", "chat_go"]

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_FILE = {"Go2": os.path.join(REPO, "assets", "manifests", "Go2.air_modules.json")}


def is_air(robot):
    try:
        return robot.info().edition == "AIR"
    except Exception:
        return False


def catalog(platform="Go2"):
    """The per-firmware catalog (schema 2), or None."""
    path = CATALOG_FILE.get(platform)
    if path and os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return None


def catalog_for(robot):
    """The catalog's module list for this robot's firmware, or None."""
    cat = catalog(robot.platform)
    if not cat:
        return None
    fw = robot.firmware_version()[0]
    return cat.get("versions", {}).get(fw)


def modules_in_package(pkg_dir, manifest):
    """Module names the package carries (manifest['modules'] filtered to those
    actually present under <pkg_dir>/modules/<name>/module.json)."""
    names = manifest.get("modules") or []
    return [n for n in names
            if os.path.isfile(os.path.join(pkg_dir, "modules", n, "module.json"))]


def status(robot):
    """Which target modules are already on the robot (dir presence)."""
    return [{"name": n, "present": os.path.isdir(robot.root + f"/unitree/module/{n}")}
            for n in TARGET_MODULES]


def _gate(robot, force, log):
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
    return True


def install_from_package(robot, pkg_dir, manifest, assume_yes=False, force=False,
                         confirm=input, log=print):
    """Install the Pro-only modules bundled in an already-extracted secdev
    package. Returns True if a power-cycle is warranted."""
    if not _gate(robot, force, log):
        return False
    names = modules_in_package(pkg_dir, manifest)
    if not names:
        log("This package carries no Air modules.")
        return False
    inst = module_installer.ModuleInstaller(root=robot.root)
    pf = inst.preflight(pkg_dir, names)
    log("\nModules to install (build matched to this firmware):")
    for r in pf:
        tag = "already present" if r["present"] == r["files"] else \
              (f"{r['present']}/{r['files']} files present" if r["present"] else "new")
        log(f"  {r['name']:20s} v{r['version']:12s} {r['files']:4d} files "
            f"{r['bytes'] / 1e6:6.1f} MB  ({tag})")
    if not assume_yes:
        ans = confirm("\nInstall these modules? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            log("Cancelled.")
            return False
    _, results = inst.install(pkg_dir, names, log=log)
    done = [r["name"] for r in results if r["action"] == "installed"]
    log(f"\nInstalled {len(done)} module(s): {', '.join(done)}")
    log("Power-cycle the robot (turn OFF, then ON) to load them.")
    return True


def remove_from_package(robot, pkg_dir, manifest, assume_yes=False, confirm=input, log=print):
    """Remove the bundled Pro modules (removeservice + rm) and restore backups."""
    names = modules_in_package(pkg_dir, manifest)
    if not names:
        log("This package carries no Air modules.")
        return False
    if not assume_yes:
        ans = confirm("Remove the Air-added Pro modules? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            log("Cancelled.")
            return False
    module_installer.ModuleInstaller(root=robot.root).remove(pkg_dir, names, log=log)
    log("Removed. Power-cycle the robot to finish.")
    return True
