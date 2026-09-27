"""
Package registry: where each platform/firmware's secondary-dev package lives.

Fill zip_link with the Yandex Disk PUBLIC link after uploading
out/r1/unitree_secondary_dev_r1_1.4.2.zip. zip_md5 is the sidecar value printed by
package_r1.py. Until zip_link is set, the tool can still install from a LOCAL path
(menu: "Install from local package" / --package <dir-or-zip>).
"""

# zip_md5 is the sidecar printed by the packager / stored next to each zip in
# orange_pi_workbench/packages/<robot>/<ver>/<pkg>.zip.md5. Fill zip_link with the
# Yandex Disk PUBLIC link once the zip is uploaded; until then, install from a LOCAL
# path (menu: "Install from local package" / --package <dir-or-zip>).
PACKAGES = {
    "R1": {
        "1.4.2": {
            "zip_link": "https://disk.yandex.com/d/Tl8GczEd9Z78NA",
            "zip_md5": "978ec5406542a5ecc2c44bd5e6d5213f",
            # optional: a standalone manifest.json link (small, for a pre-download preview)
            "manifest_link": "",
        },
    },
    "G1": {
        "1.4.0": {"zip_link": "", "zip_md5": "f33af6c7532381639fc9cd763d0ec79e", "manifest_link": ""},
        "1.4.5": {"zip_link": "", "zip_md5": "ccb9695a68de3e6ec7821f828d5f47ba", "manifest_link": ""},
        "1.5.3": {"zip_link": "", "zip_md5": "c04296105563c398f42df4cfa5963b01", "manifest_link": ""},
        "1.5.4": {"zip_link": "", "zip_md5": "f34215e4f2a931d890c43164bad5df30", "manifest_link": ""},
    },
    "Go2": {
        "1.1.2":  {"zip_link": "", "zip_md5": "88ec9819223904d5649ef36e0d95ccc7", "manifest_link": ""},
        "1.1.3":  {"zip_link": "", "zip_md5": "f2f8fd41e7f68a8953e599c1860906fa", "manifest_link": ""},
        "1.1.4":  {"zip_link": "", "zip_md5": "06afbb2705623b51abf345d60218dc21", "manifest_link": ""},
        "1.1.6":  {"zip_link": "", "zip_md5": "1834e63c4b3919cf03cf33b1554e1e58", "manifest_link": ""},
        "1.1.7":  {"zip_link": "", "zip_md5": "659623fdd1bffc5818cb300b356873a4", "manifest_link": ""},
        "1.1.11": {"zip_link": "", "zip_md5": "55b50aaa31fbec7cace6a76ec4b80851", "manifest_link": ""},
        "1.1.14": {"zip_link": "", "zip_md5": "b7f8230cfd416fc69a478af22e4c13e6", "manifest_link": ""},
        "1.1.15": {"zip_link": "", "zip_md5": "1fbd7b52f4d4074f09012cbb54b24302", "manifest_link": ""},
    },
}

DOWNLOAD_DIR = "/unitree/tmp/fwtools_downloads"


def package_for(platform, firmware):
    return PACKAGES.get(platform, {}).get(firmware)
