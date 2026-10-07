"""
yandex.py — fetch the manifest + package zip from a Yandex Disk public link.

A public link (https://disk.yandex.*/d/XXXX) is resolved to a direct download URL
via the public API, then streamed with a progress bar and md5-verified. Small JSON
resources (the standalone manifest) are fetched straight to memory.
"""
import hashlib
import json
import os
import urllib.parse
import urllib.error
import urllib.request
import zipfile

try:
    from tqdm import tqdm
except Exception:  # tqdm is optional; degrade to no progress bar
    tqdm = None

API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"


# stdlib only (urllib): stock robot images have no `requests` and may be offline.
def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "unitree_firmware_tools"})
    return urllib.request.urlopen(req, timeout=timeout)


def _direct_url(public_link, path=None):
    params = {"public_key": public_link}
    if path:
        params["path"] = path
    try:
        with _get(API + "?" + urllib.parse.urlencode(params), 30) as r:
            return json.loads(r.read())["href"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Yandex API error {e.code}: {e.read()[:200]!r}")


def fetch_json(public_link, path=None):
    """Download a small JSON resource (e.g. manifest.json) into a dict."""
    url = _direct_url(public_link, path)
    with _get(url, 60) as r:
        return json.loads(r.read())


def _md5(path, _b=1 << 16):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_b), b""):
            h.update(chunk)
    return h.hexdigest()


def download(public_link, dest_dir, path=None, expect_md5=None, filename=None):
    """Download a file from a Yandex public link into dest_dir.

    Reuses an already-present, md5-valid file. Returns the local path.
    """
    os.makedirs(dest_dir, exist_ok=True)
    url = _direct_url(public_link, path)
    if not filename:
        try:
            filename = urllib.parse.unquote(url.split("filename=")[1].split("&")[0])
        except Exception:
            filename = "download.bin"
    save_path = os.path.join(dest_dir, filename)

    if expect_md5 and os.path.isfile(save_path) and _md5(save_path) == expect_md5:
        print(f"Using cached {filename} (md5 ok).")
        return save_path

    with _get(url, 60) as resp, open(save_path, "wb") as f:
        total = int(resp.headers.get("content-length") or 0)
        bar = tqdm(desc="Downloading", total=total, unit="B", unit_scale=True,
                   unit_divisor=1024, dynamic_ncols=True) if tqdm else None
        for chunk in iter(lambda: resp.read(1 << 16), b""):
            f.write(chunk)
            if bar:
                bar.update(len(chunk))
    if bar:
        bar.close()

    if expect_md5:
        got = _md5(save_path)
        if got != expect_md5:
            os.remove(save_path)
            raise RuntimeError(f"download md5 mismatch: got {got}, want {expect_md5}")
    return save_path


def extract_zip(zip_path, dest_dir):
    """Extract a package zip into dest_dir (guards against path traversal)."""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        base = os.path.realpath(dest_dir)
        for name in z.namelist():
            target = os.path.realpath(os.path.join(dest_dir, name))
            if not target.startswith(base + os.sep) and target != base:
                raise RuntimeError(f"unsafe path in zip: {name}")
        z.extractall(dest_dir)
    return dest_dir
