"""
service_control.py — stop the running robot stack before patching, exactly the
way an official Unitree OTA does.

The real OTA (package.json -> Install.CmdPreList) stops each service with
`/unitree/sbin/mscli stopservice <name>` in leaf->core order, tolerating the
"not running / already stopped" exit codes {1,108,120}, then runs CmdPostList
(`pkill -f '/bin/login -p --'`). Patched files take effect on reboot; stopping
first prevents a live service from clobbering the file we just replaced.

The stop order is bundled per-platform in assets/service_stop/<PLATFORM>.json
(sourced verbatim from the OTA package). master_service is NOT in the OTA list
(the OTA runs *inside* master); this tool patches from outside, so it also stops
master_service when asked (`include_master`).
"""
import json
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_DIR = os.path.join(REPO, "assets", "service_stop")


def _run(cmd, timeout=30):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def load_stop_plan(platform):
    """Bundled OTA stop plan for a platform, or None if not shipped."""
    path = os.path.join(STOP_DIR, f"{platform}.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _stop_one(mscli, name, expect_ok, log):
    r = _run(f"{mscli} stopservice {name}")
    ok = r.returncode in expect_ok
    log(f"  {'✓' if ok else '!'} stopservice {name} (rc={r.returncode})"
        + ("" if ok else f"  {r.stderr.strip()[:80]}"))
    return ok


def stop_master(log=print):
    """Stop master_service itself (it is the stack supervisor)."""
    for cmd in ("service master_service stop",
                "systemctl stop master_service",
                "/unitree/sbin/mscli stopservice master_service"):
        r = _run(cmd)
        if r.returncode == 0:
            log(f"  ✓ {cmd}")
            break
    else:
        # last resort: it may be a bare process (bench) — leave a clear note
        r = _run("pkill -x master_service")
        log(f"  {'✓' if r.returncode == 0 else '·'} pkill -x master_service (rc={r.returncode})")
    # settle so children fully exit before we replace files
    _run("sleep 2")


# --- safe lie-down before stopping/flashing (quadruped) --------------------
# Mirrors the Go2 OTA CmdPreList (package.json): stop the locomotion services
# so they don't fight the move, then run basic_demarcate to walk every joint to
# its zero (lie-down) position WHILE motion_switcher/basic_service are still up.
# After this the dog is on the ground, so releasing the motors can't drop it.
CROUCH_TOOL = "/unitree/robot/tool/basic_demarcate"
CROUCH_CMD = CROUCH_TOOL + " Start_Move_zero_position"
CROUCH_SPORT = ("sport_mode", "advanced_sport", "ai_sport")
CROUCH_OK = {0, 3}                 # the OTA tolerates exit 0 or 3


def crouch(platform, log=print):
    """Lower a Go2 to the ground (zero position) the OTA way, before the stack
    is stopped or the robot reboots to flash. Returns True if the move ran;
    a no-op (False) on non-Go2 platforms or when the demarcate tool is absent."""
    if platform != "Go2":
        log(f"  · crouch skipped (no zero-position move for {platform})")
        return False
    if not os.path.exists(CROUCH_TOOL):
        log(f"  · crouch skipped ({CROUCH_TOOL} not found)")
        return False
    mscli = "/unitree/sbin/mscli"
    log("Crouching to the ground (OTA zero-position move)…")
    for name in CROUCH_SPORT:              # stop services that would fight the move
        _stop_one(mscli, name, {0, 1, 108, 120}, log)
    r = _run(CROUCH_CMD, timeout=60)
    ok = r.returncode in CROUCH_OK
    log(f"  {'✓' if ok else '!'} {CROUCH_CMD} (rc={r.returncode})"
        + ("" if ok else f"  {r.stderr.strip()[:80]}"))
    return ok


def stop_stack(platform, include_master=True, log=print, crouch_first=False):
    """Stop the robot stack the OTA way. Returns True if the plan ran.

    include_master: also stop master_service (default True — full stop, so no
    running service can rewrite a patched file before the reboot).
    crouch_first: on a Go2, lower it to the ground before stopping the stack.
    """
    plan = load_stop_plan(platform)
    if not plan:
        log(f"  ! no bundled stop plan for {platform}; skipping stack stop")
        return False
    mscli = plan.get("mscli", "/unitree/sbin/mscli")
    expect_ok = set(plan.get("expect_ok", [0, 1, 108, 120]))
    if not os.path.exists(mscli):
        log(f"  ! {mscli} not found — is this a real robot with /unitree mounted?")
        return False

    if crouch_first:
        crouch(platform, log=log)
    log("Stopping the robot stack (OTA CmdPreList order)…")
    for name in plan.get("stop_order", []):
        _stop_one(mscli, name, expect_ok, log)
    if include_master:
        log("Stopping master_service…")
        stop_master(log=log)
    for cmd in plan.get("post", []):
        r = _run(cmd)
        log(f"  · post: {cmd} (rc={r.returncode})")
    log("Stack stopped.")
    return True
