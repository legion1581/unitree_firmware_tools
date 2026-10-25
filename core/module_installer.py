"""
module_installer.py — install whole Unitree *modules* (not just patched binaries)
from a redistributable payload, the OTA way.

Where core.installer swaps a handful of md5-gated service binaries in place, this
engine adds complete modules a robot's edition never shipped (e.g. the Pro-only
ai_sport / chat_go / utrack / unitree_lidar_slam / advanced_sport / 4gcm on an
Air). Each module keeps Unitree's own on-robot layout:

    <payload>/manifest.json                 # which modules, versions, sizes
    <payload>/modules/<name>/module.json    # verbatim OTA module manifest
    <payload>/modules/<name>/file/unitree/… # the file tree, rooted at /unitree

module.json drives everything: FileList maps each absolute /unitree/… dest to its
octal mode, and Install.CmdPreList / CmdPostList are the mscli stop/reload calls
the OTA runs. We back up anything already on the robot (so install is reversible),
copy every file at its declared mode, then register the service with
`mscli reloadservice` — exactly what an OTA module install does.

On a cold image (root prefix set, or no mscli) we still lay down the files,
including the master_service descriptor, so the service registers itself at the
next boot; the mscli calls are skipped with a note.
"""
import json
import os
import re
import subprocess
import time

from . import fsutil

MSCLI = "/unitree/sbin/mscli"


def _run(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def load_payload(pkg_dir):
    """Load the payload's top manifest.json (schema 1)."""
    with open(os.path.join(pkg_dir, "manifest.json")) as f:
        m = json.load(f)
    if m.get("schema") != 1 or "modules" not in m:
        raise ValueError(f"unrecognized air-module payload manifest in {pkg_dir}")
    return m


def _module_dir(pkg_dir, name):
    return os.path.join(pkg_dir, "modules", name)


def load_module_manifest(pkg_dir, name):
    with open(os.path.join(_module_dir(pkg_dir, name), "module.json")) as f:
        return json.load(f)


def _load_modes(pkg_dir, name):
    """dest -> octal mode string, from the payload's modes.json sidecar.

    module.json's FileList maps dest -> SIZE (not mode), and zip extraction drops
    Unix perms, so the build captured a clean mode per file (0755 exec / 0644)."""
    path = os.path.join(_module_dir(pkg_dir, name), "modes.json")
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _entries(mj, modes):
    """Yield (dest_abspath, size, octal_mode) from a module.json FileList.

    FileList value is the expected byte size; mode comes from modes.json (default
    0644, or 0755 if the file can't be classified)."""
    for dest, size in mj["FileList"].items():
        mode = fsutil.parse_mode(modes.get(dest, "0644"))
        yield dest, (size if isinstance(size, int) else None), mode


class ModuleInstaller:
    def __init__(self, root="", backup_base=None):
        # root is a sandbox/image prefix; "" means the live robot.
        self.root = root.rstrip("/")
        self.backup_base = backup_base or (self.root + "/unitree/.fwtools_backup")

    def _dest(self, dest):
        return self.root + dest

    def _have_mscli(self):
        # mscli only talks to a LIVE master_service, so it is meaningless against
        # an image prefix — then the descriptor file is enough for boot-time reg.
        return not self.root and os.path.exists(MSCLI)

    # ---- inspect ------------------------------------------------------------
    def preflight(self, pkg_dir, names):
        """Per-module verdict without changing anything.

        Returns a list of {name, version, files, present, bytes, pkg_ok} where
        `present` counts how many of the module's dest files already exist on the
        robot (0 = fresh add, == files = already fully present)."""
        report = []
        manifest = {m["name"]: m for m in load_payload(pkg_dir)["modules"]}
        for name in names:
            mj = load_module_manifest(pkg_dir, name)
            modes = _load_modes(pkg_dir, name)
            ents = list(_entries(mj, modes))
            present = pkg_ok = 0
            nbytes = 0
            mdir = _module_dir(pkg_dir, name)
            for dest, size, _mode in ents:
                src = mdir + "/file" + dest
                if os.path.isfile(src) and (size is None or os.path.getsize(src) == size):
                    pkg_ok += 1
                    nbytes += os.path.getsize(src)
                if os.path.exists(self._dest(dest)):
                    present += 1
            report.append({
                "name": name, "version": mj.get("Version", "?"),
                "files": len(ents), "present": present, "pkg_ok": pkg_ok,
                "bytes": nbytes, "complete": pkg_ok == len(ents),
                "meta": manifest.get(name, {}),
            })
        return report

    # ---- install ------------------------------------------------------------
    def install(self, pkg_dir, names, dry_run=False, log=print):
        """Back up + install each named module. Returns (backup_dir, results)."""
        pf = {r["name"]: r for r in self.preflight(pkg_dir, names)}
        bad = [n for n in names if not pf[n]["complete"]]
        if bad:
            raise ValueError("payload is incomplete for: " + ", ".join(bad))

        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(self.backup_base, ts, "air_modules")
        backups = {}         # name -> [backed-up dest paths]
        results = []
        live = self._have_mscli()

        for name in names:
            mj = load_module_manifest(pkg_dir, name)
            modes = _load_modes(pkg_dir, name)
            mdir = _module_dir(pkg_dir, name)
            ents = list(_entries(mj, modes))
            if dry_run:
                log(f"  ~ {name:20s} would install {len(ents)} files "
                    f"(v{mj.get('Version','?')}, {pf[name]['present']} already present)")
                results.append({"name": name, "action": "dry-run"})
                continue

            # 1) stop the service the OTA way (tolerate not-running codes)
            if live:
                for c in mj.get("Install", {}).get("CmdPreList", []):
                    r = _run(c["Cmd"])
                    log(f"    · {c['Cmd']}  (rc={r.returncode})")

            # 2) back up any existing dest file, then copy the new one at its mode
            saved = []
            for dest, size, mode in ents:
                dpath = self._dest(dest)
                if os.path.exists(dpath):
                    bpath = os.path.join(backup_dir, name, dest.lstrip("/"))
                    cur_mode = fsutil.file_mode(dpath)
                    fsutil.copy_preserving_mode(dpath, bpath, mode=cur_mode)
                    saved.append({"dest": dest, "mode": oct(cur_mode) if cur_mode else None})
                src = mdir + "/file" + dest
                fsutil.copy_preserving_mode(src, dpath, mode=mode)
                if size is not None and os.path.getsize(dpath) != size:
                    raise ValueError(f"{name}: {dest} size {os.path.getsize(dpath)} != {size}")
            if saved:
                backups[name] = saved
            log(f"  ✓ {name:20s} installed {len(ents)} files (v{mj.get('Version','?')})"
                + (f", {len(saved)} originals backed up" if saved else ""))

            # 3) register the service with the live master (reloadservice)
            if live:
                for c in mj.get("Install", {}).get("CmdPostList", []):
                    r = _run(c["Cmd"])
                    ok = r.returncode in (c.get("ExpectCode") or [0])
                    log(f"    {'✓' if ok else '!'} {c['Cmd']}  (rc={r.returncode})")
            else:
                log(f"    · mscli skipped ({'image build' if self.root else 'mscli not found'})"
                    f" — {name} registers from its descriptor at next boot")
            results.append({"name": name, "action": "installed",
                            "version": mj.get("Version", "?"), "files": len(ents)})

        # record a backup manifest so remove()/restore can undo precisely
        if backups:
            fsutil.ensure_dir(backup_dir)
            with open(os.path.join(backup_dir, "backup_manifest.json"), "w") as f:
                json.dump({"created": ts, "kind": "air_modules",
                           "modules": {n: backups[n] for n in backups}}, f, indent=2)
        return (backup_dir if backups else None), results

    # ---- remove -------------------------------------------------------------
    def remove(self, pkg_dir, names, log=print):
        """Undo: run each module's Remove commands (stop/removeservice/rm -rf),
        then restore any originals that install() backed up."""
        live = self._have_mscli()
        for name in names:
            mj = load_module_manifest(pkg_dir, name)
            if live:
                for section in ("CmdPreList", "CmdPostList"):
                    for c in mj.get("Remove", {}).get(section, []):
                        r = _run(c["Cmd"])
                        log(f"    · {c['Cmd']}  (rc={r.returncode})")
            else:
                # no live master: mirror the OTA Remove (rm -rf the module dir +
                # its descriptor) ourselves.
                import shutil
                for dest, _size, _mode in _entries(mj, _load_modes(pkg_dir, name)):
                    try:
                        os.remove(self._dest(dest))
                    except OSError:
                        pass
                shutil.rmtree(self._dest(f"/unitree/module/{name}"), ignore_errors=True)
                try:
                    os.remove(self._dest(f"/unitree/etc/master_service/service/{name}"))
                except OSError:
                    pass
                log(f"  · {name:20s} files removed (offline)")
        self._restore_backups(names, log=log)

    def _restore_backups(self, names, log=print):
        if not os.path.isdir(self.backup_base):
            return
        for ts in sorted(os.listdir(self.backup_base), reverse=True):
            mpath = os.path.join(self.backup_base, ts, "air_modules", "backup_manifest.json")
            if not os.path.isfile(mpath):
                continue
            bm = json.load(open(mpath))
            bdir = os.path.dirname(mpath)
            for name in names:
                for fe in bm.get("modules", {}).get(name, []):
                    src = os.path.join(bdir, name, fe["dest"].lstrip("/"))
                    if os.path.isfile(src):
                        mode = fsutil.parse_mode(fe["mode"]) if fe.get("mode") else None
                        fsutil.copy_preserving_mode(src, self._dest(fe["dest"]), mode=mode)
                        log(f"  ↩ {name}: restored {fe['dest']}")
            return   # newest air_modules backup only
