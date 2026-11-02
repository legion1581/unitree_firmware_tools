"""
Package registry + tool version / self-update source.

Each robot model has ONE Yandex Disk PUBLIC *folder* link that holds every
per-firmware package zip (unitree_secondary_dev_<robot>_<ver>_v1.zip). The tool
figures out WHICH zip to pull from the model's general manifest
(assets/manifests/<Platform>.index.json) — by the robot's firmware version, or,
if that version has no dedicated package, by matching the on-disk md5 of the 4
patched targets against each version's vanilla/patched md5 set (see core.selector).

To publish a new package: upload its zip into the model's folder, add the version
to assets/manifests/<Platform>.index.json (+ its per-version <Platform>_<ver>.json
manifest). No change here is needed unless the folder link itself changes.

Until a folder link is set, the tool still installs from a LOCAL path
(menu: "Install from local package" / --package <dir-or-zip>).
"""

# model -> Yandex Disk public FOLDER link
MODEL_FOLDERS = {
    "R1":  "https://disk.yandex.com/d/-iEE_zjNr-0Byg",
    "G1":  "https://disk.yandex.com/d/VvehFZm7ZAj-Pg",
    "Go2": "https://disk.yandex.com/d/5aSwXn-0wTPhwQ",
}

DOWNLOAD_DIR = "/unitree/tmp/fwtools_downloads"

# The Pro-only modules an Air lacks (ai_sport, advanced_sport, unitree_lidar_slam,
# chat_go, utrack, 4gcm) are NOT a separate download: each Go2 secondary-dev
# package v2+ carries, next to the patch, the build of each module that a Pro of
# that firmware runs. features/air_modules.py installs them from the package, and
# assets/manifests/Go2.air_modules.json is the offline per-firmware catalog.


def folder_for(platform):
    """Yandex public folder link for a model, or '' if unset."""
    return MODEL_FOLDERS.get(platform, "")


# ---- tool version + self-update ---------------------------------------------
import os as _os

_REPO = _os.path.dirname(_os.path.abspath(__file__))

# self-update pulls from this public GitHub repo/branch (see features/updater.py)
GITHUB_SLUG = "legion1581/go2_firmware_tools"
GITHUB_BRANCH = "v2"


def version():
    """The tool version, from the repo-root VERSION file (bumped per commit)."""
    try:
        with open(_os.path.join(_REPO, "VERSION")) as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"
