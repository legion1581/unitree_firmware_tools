#!/usr/bin/env python3
"""
unitree_firmware_tools — universal tool for Unitree R1 / G1 / Go2.

Run on the robot as root:  sudo python3 app.py
"""
import argparse
import sys

import platforms
from features import info as info_feature
from features import air_modules, secondary_dev, supervisor


# ---- tiny menu helper (InquirerPy if present, else numbered fallback) --------
def menu(title, choices, extra="Quit"):
    try:
        from InquirerPy import inquirer
        return inquirer.select(message=title, choices=choices + [extra]).execute()
    except Exception:
        opts = choices + [extra]
        print(f"\n{title}")
        for i, c in enumerate(opts, 1):
            print(f"  {i}. {c}")
        while True:
            raw = input("> ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(opts):
                return opts[int(raw) - 1]
            print("Invalid choice.")


def refresh_info(robot):
    i = robot.info()
    sec = None
    try:
        sec, _ = secondary_dev.status(robot, probe_lowcmd=False)  # fast: skip live DDS probe
    except Exception:
        pass
    g = supervisor.status()
    info_feature.show(i, sec_dev_status=sec, guardian=g)
    return i


# ---- feature menus ----------------------------------------------------------
def _power_cycle_notice():
    # A soft `reboot` only cycles the Linux SoC, not the MCU/motors/power rail —
    # the stack comes back wrong. A physical power-cycle is required.
    print("\n\033[33m⚡ Power-cycle required to activate.\033[0m")
    print("   Turn the robot OFF (power button), wait a few seconds, then turn it back ON.")
    print("   (A soft reboot does NOT fully reset the MCU/motors on Unitree robots.)")


def _pick_backup(robot):
    """Return a chosen backup dict, or None to cancel."""
    backups = secondary_dev.list_backups(robot)
    if not backups:
        print("\nNo backups found. Use 'Restore factory' to install vanilla files "
              "from the package instead.")
        return None
    if len(backups) == 1:
        return backups[0]
    labels = [f"{b['ts']}  ({len(b['manifest'].get('files', []))} files"
              + (f", {b['manifest'].get('package_id')}" if b['manifest'].get('package_id') else "") + ")"
              for b in backups]
    sel = menu("Choose a restore point (newest first)", labels, extra="Cancel")
    if sel in ("Cancel", "Quit"):
        return None
    return backups[labels.index(sel)]


def secondary_dev_menu(robot, local=None, air_local=None):
    air = air_modules.is_air(robot)
    while True:
        choices = ["Status", "Enable", "Restore backup", "Restore factory"]
        if air:
            choices.append("Install Pro modules (Air)")
        choices.append("Back")
        c = menu("Secondary development", choices)
        if c == "Status":
            print("  checking (edition + DDS + rt/lowcmd)…")
            verdict, report = secondary_dev.status(robot)   # full: live lowcmd probe
            print(f"\nSecondary development: {verdict['verdict']}")
            for k, v in verdict["signals"].items():
                print(f"  · {k:14s} {v}")
            if report:
                print("\n  per-file (bundled manifest md5):")
                for r in report:
                    print(f"    {r['name']:20s} {r['verdict']}")
        elif c == "Enable":
            if secondary_dev.enable(robot, local=local, air_local=air_local):
                _power_cycle_notice()
        elif c == "Install Pro modules (Air)":
            if air_modules.install(robot, local=air_local):
                _power_cycle_notice()
        elif c == "Restore backup":
            b = _pick_backup(robot)
            if b and secondary_dev.restore_backup(robot, backup=b):
                _power_cycle_notice()
        elif c == "Restore factory":
            if secondary_dev.restore_factory(robot, local=local):
                _power_cycle_notice()
        elif c in ("Back", "Quit"):
            return c == "Quit"


def guardian_menu():
    while True:
        st = supervisor.status()
        state = ("active" if st.get("active") else "installed" if st["installed"] else "not installed")
        c = menu(f"Guardian failsafe  [{state}]", ["Status", "Install", "Uninstall", "Back"])
        if c == "Status":
            print(f"\n  installed={st['installed']} enabled={st.get('enabled')} active={st.get('active')}")
            print("  Keeps sshd alive and restores last-known-good WiFi if the stack fails to boot.")
        elif c == "Install":
            supervisor.install()
        elif c == "Uninstall":
            supervisor.uninstall()
        elif c in ("Back", "Quit"):
            return c == "Quit"


def main():
    ap = argparse.ArgumentParser(description="Unitree firmware tools (R1/G1/Go2)")
    ap.add_argument("--platform", help="force platform (R1/G1/Go2) instead of auto-detect")
    ap.add_argument("--package", help="local secondary-dev package (dir or .zip) instead of download")
    ap.add_argument("--air-modules", help="local Air Pro-modules payload (dir or .zip) instead of download")
    ap.add_argument("--root", default="", help="sandbox root prefix (testing)")
    args = ap.parse_args()

    robot = platforms.detect(root=args.root, force=args.platform)
    print(f"Detected platform: {robot.platform}")
    refresh_info(robot)

    while True:
        c = menu("Main menu", ["Robot info", "Secondary development", "Guardian failsafe"])
        if c == "Robot info":
            refresh_info(robot)
        elif c == "Secondary development":
            if secondary_dev_menu(robot, local=args.package, air_local=args.air_modules):
                break
        elif c == "Guardian failsafe":
            if guardian_menu():
                break
        elif c == "Quit":
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
        sys.exit(0)
