"""Platform detection: pick the right Robot for the box we're running on."""
import os

from .base import Robot, RobotInfo, parse_identity
from .r1 import R1Robot
from .g1 import G1Robot
from .go2 import Go2Robot

_BY_NAME = {"R1": R1Robot, "G1": G1Robot, "GO2": Go2Robot, "GO2AIR": Go2Robot}


def detect(root="", force=None):
    """Return a Robot instance. `force` (or $UNITREE_PLATFORM) overrides auto-detect."""
    force = force or os.environ.get("UNITREE_PLATFORM")
    if force:
        cls = _BY_NAME.get(force.upper().replace("_", "").replace(" ", ""))
        if not cls:
            raise ValueError(f"unknown platform override: {force}")
        return cls(root=root)

    # the firmware tree itself says which robot it is (model-only modules)
    by_tree = _from_modules(root)
    if by_tree:
        return by_tree(root=root)

    # secure-element robots (R1 / G1) expose /dev/uni_sec; Go2 uses the flash partition.
    if os.path.exists(root + "/dev/uni_sec"):
        return _hint_humanoid(root)
    if os.path.exists(root + "/dev/mmcblk0p3"):
        return Go2Robot(root=root)
    # last resort: assume R1 (the primary target) so the menu still loads
    return R1Robot(root=root)


# modules that only ship in one robot's /unitree tree (version.json's Package is
# just the version number, so it can't tell the models apart)
_MODEL_MODULES = (
    (Go2Robot, ("sport_mode", "pet_go", "utrack")),
    (G1Robot, ("g1_arm_example", "dex3_service")),
    (R1Robot, ("r1_arm_example", "ai_sport/bin/r1_controller")),
)


def _from_modules(root):
    mod = root + "/unitree/module/"
    for cls, marks in _MODEL_MODULES:
        if any(os.path.exists(mod + m) for m in marks):
            return cls
    return None


def _hint_humanoid(root):
    """R1 vs G1 both have uni_sec — disambiguate from the firmware package string."""
    try:
        import json
        with open(root + "/unitree/robot/pkg/version/version.json") as f:
            pkg = (json.load(f).get("Package") or "").upper()
        if "G1" in pkg:
            return G1Robot(root=root)
    except Exception:
        pass
    return R1Robot(root=root)
