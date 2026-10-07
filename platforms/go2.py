"""Go2: identity from /dev/uni_sec, like R1/G1 (see base.py); on Go2 the
driver is backed by the eMMC uni area."""
from .base import Robot


class Go2Robot(Robot):
    platform = "Go2"
    services_path = {
        "master_service": "/unitree/module/master_service/master_service",
        "basic_service": "/unitree/module/basic_service/basic_service",
        "basic_service_check": "/unitree/robot/tool/basic_service_check",
        "vui_service": "/unitree/module/vui_service/vui_service",
    }
