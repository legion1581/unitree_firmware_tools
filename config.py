"""
Package registry.

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

# Add-on payload: the Pro-only modules an Air lacks (ai_sport, advanced_sport,
# unitree_lidar_slam, chat_go, utrack, 4gcm), newest build of each. Lives in the
# same model folder as the secondary-dev packages. See features/air_modules.py
# and the bundled catalog assets/manifests/<Platform>.air_modules.json.
AIR_MODULES = {
    "Go2": {
        "zip": "unitree_go2_air_modules_v1.zip",
        "zip_md5": "756b6c1d74fec80fa9c704bf12102342",
        "folder": MODEL_FOLDERS["Go2"],
    },
}


def folder_for(platform):
    """Yandex public folder link for a model, or '' if unset."""
    return MODEL_FOLDERS.get(platform, "")


def air_modules_for(platform):
    """Air-modules payload spec for a model, or None if unset."""
    return AIR_MODULES.get(platform)
