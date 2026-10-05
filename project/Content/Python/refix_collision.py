# -*- coding: utf-8 -*-
"""
refix_collision.py -- rebuild the terrain collision proxies the right way round.

The complex-as-simple trimesh of the C_overworld_*_collision meshes is empty,
even though every flag reads correctly (bAllowCPUAccess=1,
CollisionTraceFlag=UseComplexAsSimple, sections collide, component blocks).
The cause is ordering: UStaticMesh cooks its complex collision from the *render
data*, and the render data keeps its index buffer on the CPU only when
``bAllowCPUAccess`` was set **when the render data was built**. Setting the flag
after import -- which is what set_cpu_access.py does -- leaves the already-built
render data (and its DDC entry) without the CPU index copy, so the trimesh is
empty and a complex trace misses while the bounding-box fallback still hits.

The fix is to make new imports *start* with the flag on, by setting it on the
UStaticMesh class default object, and then re-import each proxy so its render
data is built with CPU access from the outset.

Run inside the editor:
    UnrealEditor.exe MCReplica.uproject -ExecutePythonScript=<this file> \
        -unattended -nopause -nosplash
"""

import os

import unreal

DEST_DIR = "/Game/MC/Terrain"
TILES = [
    ("00_00", "Q:/MC2UE5/terrain/overworld_00_00_collision.obj"),
    ("00_01", "Q:/MC2UE5/terrain/overworld_00_01_collision.obj"),
    ("01_00", "Q:/MC2UE5/terrain/overworld_01_00_collision.obj"),
    ("01_01", "Q:/MC2UE5/terrain/overworld_01_01_collision.obj"),
]

REPORT_PATH = "Q:/MC2UE5/logs/refix_collision.txt"
_lines = []


def say(msg):
    _lines.append(msg)
    unreal.log("[MCFIX2] " + msg)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    tools = unreal.AssetToolsHelpers.get_asset_tools()

    # 1. Class default: anything constructed from now on keeps its vertex data.
    cdo = unreal.StaticMesh.get_default_object()
    try:
        cdo.set_editor_property("allow_cpu_access", True)
        say("CDO allow_cpu_access set")
    except Exception as exc:
        say("CDO set failed: %s" % exc)

    for tile, src in TILES:
        name = "C_overworld_%s_collision" % tile
        dest = "%s/%s" % (DEST_DIR, name)
        if not os.path.isfile(src):
            say("%s: missing %s" % (name, src))
            continue

        # 2. Delete so the import genuinely rebuilds rather than reusing the
        #    cached render data that was built without CPU access.
        if unreal.EditorAssetLibrary.does_asset_exist(dest):
            unreal.EditorAssetLibrary.delete_asset(dest)

        task = unreal.AssetImportTask()
        task.set_editor_property("filename", src)
        task.set_editor_property("destination_path", DEST_DIR)
        task.set_editor_property("destination_name", name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", False)
        tools.import_asset_tasks([task])

        mesh = unreal.load_asset(dest)
        if mesh is None:
            say("%s: import produced nothing" % name)
            continue

        # 3. Belt and braces: the flag on the new asset, the trace flag on its
        #    BodySetup, and the mesh-level collision complexity.
        try:
            sub.set_allow_cpu_access(mesh, True)
        except Exception:
            try:
                mesh.set_editor_property("allow_cpu_access", True)
            except Exception as exc:
                say("%s: cpu access: %s" % (name, exc))
        try:
            body = mesh.get_editor_property("body_setup")
            body.set_editor_property(
                "collision_trace_flag",
                unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
        except Exception as exc:
            say("%s: body setup: %s" % (name, exc))
        try:
            mesh.set_editor_property(
                "collision_complexity",
                unreal.CollisionComplexity.CTC_USE_COMPLEX_AS_SIMPLE)
        except Exception:
            pass

        try:
            unreal.EditorAssetLibrary.save_loaded_asset(mesh)
            saved = "saved"
        except Exception as exc:
            saved = "SAVE FAILED: %s" % exc

        say("%s verts=%s cpu=%s flag=%s %s"
            % (name, sub.get_number_verts(mesh, 0),
               mesh.get_editor_property("allow_cpu_access"),
               sub.get_collision_complexity(mesh), saved))

    say("--- done ---")


if __name__ == "__main__":
    main()
