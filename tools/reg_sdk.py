# -*- coding: utf-8 -*-
"""
reg_sdk.py -- point the Windows SDK / .NET Framework SDK registry entries at Q:.

UnrealBuildTool discovers both toolchains through the registry, and both live
under ``Q:`` on this machine rather than on ``C:``:

  * ``HKLM\\SOFTWARE\\Microsoft\\Windows Kits\\Installed Roots\\KitsRoot10``
    -> the Windows SDK (headers, libs, rc.exe)
  * ``HKLM\\SOFTWARE\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full\\NetFxSdk``
    -> the .NET Framework SDK, needed to instantiate ``SwarmInterface`` when
       building the editor target

Without the first, UBT reports "No valid Visual C++ toolchain was found" even
though MSVC is installed. Without the second, building ``<Project>Editor`` fails
with "Could not find NetFxSDK install dir".

C: is wiped on reboot in this environment, so these have to be rewritten before
every build rather than once at install time.

Uses ``winreg`` directly: ``reg.exe`` is on this machine's program blacklist, so
shelling out to it is not an option.

    python3 reg_sdk.py        # silent on success, non-zero on failure
"""

import sys

import winreg

SDK_ROOT = r"Q:\WindowsKits\\"
NETFX_ROOT = r"Q:\WindowsKits\NETFXSDK\4.6.2"

#: (hive, subkey, value name, data, type)
ENTRIES = (
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
     "KitsRoot10", SDK_ROOT, winreg.REG_SZ),
    (winreg.HKEY_CURRENT_USER,
     r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
     "KitsRoot10", SDK_ROOT, winreg.REG_SZ),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
     "NetFxSdk", NETFX_ROOT, winreg.REG_SZ),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\WOW6432Node\Microsoft\NET Framework Setup\NDP\v4\Full",
     "NetFxSdk", NETFX_ROOT, winreg.REG_SZ),
)


def main():
    failures = []

    for hive, subkey, name, data, vtype in ENTRIES:
        try:
            key = winreg.CreateKeyEx(hive, subkey, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, name, 0, vtype, data)
            winreg.CloseKey(key)
        except Exception as exc:
            failures.append("write %s\\%s: %s" % (subkey, name, exc))
            continue

        try:
            key = winreg.OpenKey(hive, subkey)
            actual = winreg.QueryValueEx(key, name)[0]
            winreg.CloseKey(key)
            if "Q:" not in str(actual):
                failures.append("verify %s\\%s: still %r"
                                % (subkey, name, actual))
        except Exception as exc:
            failures.append("verify %s\\%s: %s" % (subkey, name, exc))

    if failures:
        for f in failures:
            sys.stderr.write("reg_sdk: %s\n" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
