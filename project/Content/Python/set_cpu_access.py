# -*- coding: utf-8 -*-
"""
set_cpu_access.py -- make the terrain collision proxies keep CPU vertex data.

Root cause of "the pawn falls through the campus": the four C_overworld_*_collision
static meshes were imported with bAllowCPUAccess = false. Complex-as-simple
collision cooks its physics trimesh from the mesh's CPU-accessible render data;
with bAllowCPUAccess false that copy is released after GPU upload, so
CreatePhysicsMeshes() produces an empty trimesh and the character's complex
floor trace misses. Flipping the flag and re-saving the assets makes the load
path retain the CPU buffer, so the runtime cook succeeds and the pawn stands on
the terrain.

This is the persistent fix for already-built levels. The build pipeline
(build_release_level.py) sets the same flag at import time so fresh builds are
correct without this script.

Run inside the editor:
    UnrealEditor.exe MCReplica.uproject -ExecutePythonScript=<this file> -unattended -nopause -nosplash
"""

import os
import unreal

# A marker file the launcher polls so it can tell when the script finished and
# whether the save actually persisted, independent of log flushing.
RESULT_PATH = "Q:/MC2UE5/repo/cpuaccess_result.txt"


def _write_result(status, detail):
    try:
        with open(RESULT_PATH, "w") as fh:
            fh.write("%s\n%s\n" % (status, detail))
    except Exception:
        pass


MESHES = [
    "/Game/MC/Terrain/C_overworld_00_00_collision",
    "/Game/MC/Terrain/C_overworld_00_01_collision",
    "/Game/MC/Terrain/C_overworld_01_00_collision",
    "/Game/MC/Terrain/C_overworld_01_01_collision",
]

# UHT reflects bAllowCPUAccess as b_allow_cpu_access; older/newer bindings may
# differ, so try each and stop at the first that the class actually exposes.
CANDIDATES = ["b_allow_cpu_access", "allow_cpu_access", "bAllowCPUAccess"]


def _cpu_access_property(mesh):
    for name in CANDIDATES:
        try:
            mesh.get_editor_property(name)
            return name
        except Exception:
            continue
    return None


def main():
    done = 0
    used_prop = None
    for path in MESHES:
        mesh = unreal.load_asset(path)
        if mesh is None:
            unreal.log_error("[MC2UE5] could not load %s" % path)
            continue
        prop = _cpu_access_property(mesh)
        if prop is None:
            unreal.log_error("[MC2UE5] no CPU-access property on %s" % path)
            continue
        used_prop = prop
        try:
            mesh.set_editor_property(prop, True)
        except Exception as exc:
            unreal.log_error("[MC2UE5] could not set %s on %s: %s"
                             % (prop, mesh.get_name(), exc))
            continue
        try:
            unreal.EditorAssetLibrary.save_loaded_asset(mesh)
        except Exception as exc:
            unreal.log_error("[MC2UE5] save failed for %s: %s" % (path, exc))
            continue
        unreal.log("[MC2UE5] %s: %s=True (saved)" % (mesh.get_name(), prop))
        done += 1
    unreal.log("[MC2UE5] cpu-access set on %d/%d collision proxies"
               % (done, len(MESHES)))
    return done == len(MESHES), used_prop, done


if __name__ == "__main__":
    ok, prop, count = main()
    status = "OK" if ok else "INCOMPLETE"
    detail = "prop=%s set_on=%d/4" % (prop, count)
    unreal.log("[MC2UE5] set_cpu_access %s (%s)" % (status, detail))
    _write_result(status, detail)
