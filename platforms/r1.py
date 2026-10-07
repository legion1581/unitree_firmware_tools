"""R1 humanoid: identity from the /dev/uni_sec secure element (see base.py)."""
from .base import Robot


class R1Robot(Robot):
    platform = "R1"
    services_path = {
        "master_service": "/unitree/module/master_service/master_service",
        "basic_service": "/unitree/module/basic_service/basic_service",
        "basic_service_check": "/unitree/robot/tool/basic_service_check",
        "net-init": "/unitree/etc/master_service/cmd/net-init",
    }
