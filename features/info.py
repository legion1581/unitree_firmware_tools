"""Render the universal robot info card."""

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
     "r": "\033[31m", "c": "\033[36m", "x": "\033[0m"}


def _c(s, col):
    return f"{C[col]}{s}{C['x']}"


def render(info, sec_dev_status=None, guardian=None):
    lines = []
    title = f"{info.platform} {info.edition}".strip()
    lines.append(_c(f"  {title}", "b"))
    lines.append(_c("  " + "─" * 34, "dim"))

    def row(k, v, col=None):
        v = "—" if v in (None, "") else v
        lines.append(f"  {k:<14}{_c(v, col) if col else v}")

    row("Serial", info.sn)
    row("Region", info.region)
    row("Hardware", f"v{info.hardware}" if info.hardware else "")
    row("Bluetooth", info.bluetooth)
    fw = info.firmware + (f"  (mod {info.firmware_mod})" if info.firmware_mod else "")
    row("Firmware", fw)
    sb = info.secure_boot
    row("Secure boot", "unknown" if sb is None else
        ("enabled (signed loader, RKSS)" if sb else "disabled (unsigned loader, RKNS)"),
        "y" if sb else ("dim" if sb is None else "g"))

    if sec_dev_status is not None:
        en = sec_dev_status.get("enabled") if isinstance(sec_dev_status, dict) else sec_dev_status
        label = {True: "enabled", False: "disabled", None: "unknown"}[en]
        row("Secondary dev", label, "g" if en else "dim")
    if guardian is not None:
        if guardian.get("installed"):
            g = "active" if guardian.get("active") else "installed (inactive)"
            row("Guardian", g, "g" if guardian.get("active") else "y")
        else:
            row("Guardian", "not installed", "dim")

    if info.extra.get("identity_error"):
        lines.append("  " + _c("! " + info.extra["identity_error"], "r"))
    return "\n".join(lines)


def show(info, **kw):
    print()
    print(render(info, **kw))
    print()
