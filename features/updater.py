"""
updater.py — self-update the tool from the public GitHub repo.

`check()` compares the local VERSION against the VERSION file on the configured
branch; `apply()` downloads that branch's tarball and copies the code over the
install dir. Persistent state lives in /etc/unitree-fwtools (auth, prefs), not in
the install dir, so an update never touches the password or settings. Runtime
dirs (.git, __pycache__, downloads) are left alone.
"""
import io
import os
import shutil
import tarfile
import tempfile
import urllib.request

import config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # install dir
RAW = "https://raw.githubusercontent.com/{slug}/{branch}/VERSION"
TARBALL = "https://codeload.github.com/{slug}/tar.gz/refs/heads/{branch}"
SKIP = {".git", "__pycache__", "downloads"}        # never overwritten
UA = {"User-Agent": "unitree-fwtools-updater"}


def _vtuple(s):
    try:
        return tuple(int(x) for x in str(s).strip().split("."))
    except (TypeError, ValueError):
        return (0,)


def _get(url, timeout):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:   # follows redirects
        return r.read()


def latest_version():
    url = RAW.format(slug=config.GITHUB_SLUG, branch=config.GITHUB_BRANCH)
    return _get(url, 15).decode().strip()


def check():
    """{current, latest, update_available, error}."""
    cur = config.version()
    try:
        lat = latest_version()
    except Exception as e:
        return {"current": cur, "latest": None, "update_available": False,
                "error": f"could not reach GitHub: {e}"}
    if not lat or not lat[0].isdigit():
        return {"current": cur, "latest": None, "update_available": False,
                "error": "remote has no VERSION file yet"}
    return {"current": cur, "latest": lat, "error": None,
            "update_available": _vtuple(lat) > _vtuple(cur)}


def _safe_members(tar, base):
    """Yield members that stay within `base` (guard against path traversal)."""
    base = os.path.realpath(base)
    for m in tar.getmembers():
        if m.isdev():
            continue
        dest = os.path.realpath(os.path.join(base, m.name))
        if dest == base or dest.startswith(base + os.sep):
            yield m


def _copy_tree(src, dst, log=None):
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP]
        rel = os.path.relpath(root, src)
        tgt = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(tgt, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), os.path.join(tgt, f))
            n += 1
    return n


def apply(log=None, force=False):
    """Download the branch tarball and copy it over the install dir.
    Refuses to apply a version that isn't newer unless `force`. Returns the
    installed version string."""
    info = check()
    if info["error"] and not force:
        raise IOError(info["error"])
    if not force and not info["update_available"]:
        raise IOError(f"already up to date ({info['current']})")
    url = TARBALL.format(slug=config.GITHUB_SLUG, branch=config.GITHUB_BRANCH)
    if log:
        log(f"downloading {url}")
    data = _get(url, 120)
    if log:
        log(f"downloaded {len(data)} bytes; extracting")
    tmp = tempfile.mkdtemp(prefix="fwtools-upd-")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            t.extractall(tmp, members=list(_safe_members(t, tmp)))
        tops = [os.path.join(tmp, d) for d in os.listdir(tmp)]
        src = next((d for d in tops if os.path.isdir(d)), None)
        if not src:
            raise IOError("unexpected tarball layout")
        new_ver = "?"
        try:
            with open(os.path.join(src, "VERSION")) as f:
                new_ver = f.read().strip()
        except OSError:
            pass
        n = _copy_tree(src, REPO, log)
        if log:
            log(f"installed {n} files; now version {new_ver}")
        return new_ver
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
