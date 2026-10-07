"""
base.py — the universal robot model.

Identity is one 29-byte ASCII "Hardware" block, identical across R1/G1/Go2:
    [0:16]  serial number      (edition is derived from sn[4])
    [16:18] country/region
    [18:20] hardware rev        ("10" -> "1.0")
    [20:25] bluetooth code
    [25:29] reserved
Every robot (R1, G1 and Go2) exposes it through the /dev/uni_sec driver: an
ioctl returns a 1024-byte secure blob with the block at offset 0x100 (on Go2
the driver is backed by the eMMC "uni" area). That is the only source: if
/dev/uni_sec is missing or unreadable, the identity is reported as unavailable.
"""
import fcntl
import json
import os
import re
from dataclasses import dataclass, field

EDITION = {1: "AIR", 2: "PRO", 4: "EDU", 3: "MAX"}

# /dev/uni_sec ioctls (fixed 1024-byte secure blob; _IOC size field ignored)
IOCTL_SECURE_RSA_R = 0x80045202  # read blob
UNISEC_DEV = "/dev/uni_sec"
UNISEC_BLOB_LEN = 1024
HW_OFF = 0x100   # 29-byte ASCII Hardware block lives here in the blob
HW_LEN = 29

# 16-char serial + 2-letter region; anything else is not an identity block
_IDENTITY_RE = re.compile(rb"[A-Z0-9]{16}[A-Z]{2}")


def read_uni_sec_blob(dev=UNISEC_DEV):
    buf = bytearray(UNISEC_BLOB_LEN)
    fd = os.open(dev, os.O_RDWR)
    try:
        fcntl.ioctl(fd, IOCTL_SECURE_RSA_R, buf, True)
    finally:
        os.close(fd)
    return bytes(buf)


def identity_valid(block):
    """True if block starts like a real identity (so an empty secure element
    isn't shown as a garbage serial/region)."""
    return bool(block) and bool(_IDENTITY_RE.match(bytes(block[:18])))


# Rockchip idblock (v2) header magic at sector 64 of the boot device
LOADER_SIGNED = b"RKSS"      # loader signed for secure boot
LOADER_UNSIGNED = b"RKNS"    # plain loader


def loader_magic(root="", device=None):
    """The 4-byte loader header magic at sector 64 (offset 0x8000) of the eMMC,
    or None if the device can't be read. Read-only 4-byte read, never writes."""
    cands = [device] if device else []
    cands += [root + "/dev/mmcblk0", "/dev/mmcblk0"]
    for d in cands:
        if not d:
            continue
        try:
            with open(d, "rb") as f:          # opened O_RDONLY
                f.seek(64 * 512)
                return f.read(4)
        except OSError:
            continue
    return None


def rkss_secure_boot(root="", device=None):
    """True if the loader is signed (RKSS), False if it is a plain one (RKNS),
    None if unreadable or neither (unknown, never assumed off).

    This shows how the loader on flash was packed. Whether the SoC enforces the
    signature is a fuse in the secure OTP, which Linux cannot read; a signed
    loader is shipped because that fuse is burned, so RKSS is a strong indicator.
    """
    return {LOADER_SIGNED: True, LOADER_UNSIGNED: False}.get(loader_magic(root, device))


@dataclass
class RobotInfo:
    platform: str = "?"
    sn: str = ""
    edition: str = "?"
    region: str = ""
    hardware: str = ""
    bluetooth: str = ""
    firmware: str = ""
    firmware_mod: str = ""
    secure_boot: bool = None      # True / False / None = unknown (couldn't read)
    identity_source: str = ""
    extra: dict = field(default_factory=dict)


def parse_identity(block):
    """Parse a >=25-byte ASCII identity block into fields."""
    b = block.decode("ascii", "replace") if isinstance(block, (bytes, bytearray)) else block
    sn = b[0:16].strip("\x00 ")
    edition = "?"
    if len(sn) >= 5 and sn[4].isdigit():
        edition = EDITION.get(int(sn[4]), "?")
    hw_raw = b[18:20].strip("\x00 ")
    hardware = f"{hw_raw[0]}.{hw_raw[1]}" if len(hw_raw) == 2 else hw_raw
    return {
        "sn": sn,
        "edition": edition,
        "region": b[16:18].strip("\x00 "),
        "hardware": hardware,
        "bluetooth": b[20:25].strip("\x00 "),
    }


class Robot:
    platform = "?"
    services_path = {}                       # name -> on-robot destination path
    version_json = "/unitree/robot/pkg/version/version.json"

    def __init__(self, root=""):
        self.root = root.rstrip("/")

    # --- per platform ---
    def read_secure_boot(self):
        return rkss_secure_boot(self.root)

    # --- shared ---
    def read_identity_block(self):
        """The 29-byte ASCII identity block from /dev/uni_sec (raise if unavailable)."""
        dev = self.root + UNISEC_DEV
        if not os.path.exists(dev):
            raise RuntimeError(f"{UNISEC_DEV} is not available")
        try:
            block = read_uni_sec_blob(dev)[HW_OFF:HW_OFF + HW_LEN]
        except OSError as e:
            raise RuntimeError(f"{UNISEC_DEV} could not be read ({e.strerror or e})")
        if not identity_valid(block):
            raise RuntimeError(f"{UNISEC_DEV} holds no identity")
        return block

    def firmware_version(self):
        try:
            with open(self.root + self.version_json) as f:
                j = json.load(f)
            return j.get("Package", ""), j.get("Mod", "")
        except Exception:
            return "", ""

    def info(self):
        i = RobotInfo(platform=self.platform)
        try:
            block = self.read_identity_block()   # may update self.identity_source
            fields = parse_identity(block)
            i.sn = fields["sn"]; i.edition = fields["edition"]; i.region = fields["region"]
            i.hardware = fields["hardware"]; i.bluetooth = fields["bluetooth"]
        except Exception as e:
            i.extra["identity_error"] = str(e)
        i.identity_source = self.identity_source
        i.firmware, i.firmware_mod = self.firmware_version()
        try:
            i.secure_boot = self.read_secure_boot()
            m = loader_magic(self.root)
            i.extra["loader_magic"] = (m.decode("ascii", "replace") if m and m.isalnum() else
                                       m.hex() if m else None)
        except Exception:
            pass
        return i

    identity_source = "uni_sec"
