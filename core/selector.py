"""
selector.py — pick the right secondary-dev package for the robot in front of us.

Each model ships a "general manifest" (assets/manifests/<Platform>.index.json)
listing every packaged firmware version with, per target file, its vanilla
(original) and patched md5. Selection order:

  1. by firmware version string  — the fast path (version.json matches a package).
  2. by md5 fingerprint          — read the 4 target files on the device and find
     the version whose *vanilla* set matches (secondary-dev not yet enabled) or
     whose *patched* set matches (already enabled). This handles a robot whose
     reported version has no dedicated package but whose binaries are byte-for-byte
     one we do package (many Unitree releases ship identical service binaries).

Many versions share byte-identical binaries, so a fingerprint can match a whole
group; they are interchangeable (same patched + same vanilla + same factory), so
any member installs correctly. We prefer the member whose firmware string equals
the device's reported version, else the newest.
"""
import json
import os
import re

from . import fsutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_DIR = os.path.join(REPO, "assets", "manifests")


def _verkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def load_index(platform):
    """Load the per-model general manifest, or None if not shipped."""
    path = os.path.join(MANIFEST_DIR, f"{platform}.index.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        idx = json.load(f)
    if idx.get("schema") != 1 or "versions" not in idx:
        return None
    return idx


def device_fingerprint(robot, index):
    """{target_name: current_md5_or_None} for the index's target files."""
    root = robot.root
    # dest paths are the same across a model's versions; take them from any entry.
    dests = {}
    for ver in index["versions"]:
        for name, fe in ver["files"].items():
            dests.setdefault(name, fe["dest"])
    return {name: fsutil.md5(root + dest) for name, dest in dests.items()}


def _match_state(ver, fp):
    """Return 'vanilla' / 'installed' / None for how the device fingerprint fp
    matches this version entry (all targets must agree)."""
    files = ver["files"]
    if any(fp.get(n) is None for n in files):
        return None
    if all(fp[n] == fe["original_md5"] for n, fe in files.items()):
        return "vanilla"
    if all(fp[n] == fe["patched_md5"] for n, fe in files.items()):
        return "installed"
    return None


def select(robot, index=None, firmware=None):
    """Choose a package for this robot.

    Returns a dict:
      {version, entry, how, state, matches, fingerprint}
    or None if nothing matches. `how` is 'version' or 'fingerprint'; `state` is
    'vanilla'/'installed'/None (None when chosen purely by version string on a
    device whose files we couldn't read/aren't a known set).
    """
    index = index or load_index(robot.platform)
    if not index:
        return None
    fw = firmware if firmware is not None else robot.firmware_version()[0]
    by_ver = {v["firmware"]: v for v in index["versions"]}
    fp = device_fingerprint(robot, index)

    # 1) exact firmware-version match
    if fw in by_ver:
        entry = by_ver[fw]
        return {"version": fw, "entry": entry, "how": "version",
                "state": _match_state(entry, fp), "matches": [fw], "fingerprint": fp}

    # 2) md5 fingerprint match across all versions
    matched = [(v["firmware"], _match_state(v, fp)) for v in index["versions"]]
    matched = [(f, s) for f, s in matched if s]
    if not matched:
        return None
    # prefer a member whose firmware equals the reported version, else newest.
    matched.sort(key=lambda t: (t[0] != fw, _verkey(t[0])), reverse=False)
    # newest among ties: re-sort so exact-fw first, then highest version
    matched.sort(key=lambda t: (t[0] == fw, _verkey(t[0])), reverse=True)
    best_fw, best_state = matched[0]
    return {"version": best_fw, "entry": by_ver[best_fw], "how": "fingerprint",
            "state": best_state, "matches": [f for f, _ in matched], "fingerprint": fp}
