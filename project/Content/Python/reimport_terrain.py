# -*- coding: utf-8 -*-
"""
reimport_terrain.py -- re-import the terrain meshes from the corrected OBJs.

The deployed terrain OBJs predated the normal data: their faces were written as
``f v/vt`` with no vertex normals at all, and the importer silently discarded
~90% of the geometry (a 12,502-vertex collision proxy arrived as 1,122 verts; a
196,944-vertex visual mesh as 2,179). The collision that resulted did not match
the terrain, so the pawn fell through it.

tools/build_terrain_mesh.py now writes ``f v/vt/vn`` and a matching ``vn`` list.
This re-imports all eight meshes from those OBJs and re-applies the per-mesh
settings the level builder gave them, which an import resets:

  * collision proxies -- keep vertex data CPU-side, use complex-as-simple;
  * visual meshes     -- the terrain material and Nanite.

Existing actors keep working: the assets are re-imported under the same names.

Run inside the editor:
    UnrealEditor.exe MCReplica.uproject -ExecutePythonScript=<this file> \
        -unattended -nopause -nosplash
"""

import os

import unreal

MESH_DIR = "/Game/MC/Terrain"
OBJ_DIR = "Q:/MC2UE5/terrain"
TERRAIN_MATERIAL = "/Game/MC/Materials/MC_Terrain"
REPORT_PATH = "Q:/MC2UE5/logs/reimport_terrain.txt"

TILES = ("overworld_00_00", "overworld_00_01",
         "overworld_01_00", "overworld_01_01")

_lines = []


def say(msg):
    _lines.append(msg)
    unreal.log("[MCIMP2] " + msg)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def import_obj(tools, src, name):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", src)
    task.set_editor_property("destination_path", MESH_DIR)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", False)
    tools.import_asset_tasks([task])
    return unreal.load_asset("%s/%s" % (MESH_DIR, name))


def main():
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    material = unreal.load_asset(TERRAIN_MATERIAL)

    for tile in TILES:
        # ---- collision proxy --------------------------------------------
        cname = "C_%s_collision" % tile
        csrc = os.path.join(OBJ_DIR, "%s_collision.obj" % tile)
        if os.path.isfile(csrc):
            mesh = import_obj(tools, csrc, cname)
            if mesh:
                try:
                    sub.set_allow_cpu_access(mesh, True)
                except Exception:
                    pass
                try:
                    mesh.get_editor_property("body_setup").set_editor_property(
                        "collision_trace_flag",
                        unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
                except Exception as exc:
                    say("  %s trace flag: %s" % (cname, exc))
                try:
                    mesh.set_editor_property(
                        "collision_complexity",
                        unreal.CollisionComplexity.CTC_USE_COMPLEX_AS_SIMPLE)
                except Exception:
                    pass
                unreal.EditorAssetLibrary.save_loaded_asset(mesh)
                say("%s verts=%s tris=%s" % (
                    cname, sub.get_number_verts(mesh, 0),
                    mesh.get_num_triangles(0) if hasattr(mesh, "get_num_triangles")
                    else "?"))
        else:
            say("%s: missing %s" % (cname, csrc))

        # ---- visual mesh -------------------------------------------------
        vname = "T_%s" % tile
        vsrc = os.path.join(OBJ_DIR, "%s.obj" % tile)
        if os.path.isfile(vsrc):
            mesh = import_obj(tools, vsrc, vname)
            if mesh:
                if material is not None:
                    try:
                        mesh.set_material(0, material)
                    except Exception as exc:
                        say("  %s material: %s" % (vname, exc))
                try:
                    settings = sub.get_nanite_settings(mesh)
                    settings.set_editor_property("enabled", True)
                    sub.set_nanite_settings(mesh, settings, True)
                except Exception as exc:
                    say("  %s nanite: %s" % (vname, exc))
                unreal.EditorAssetLibrary.save_loaded_asset(mesh)
                say("%s verts=%s" % (vname, sub.get_number_verts(mesh, 0)))
        else:
            say("%s: missing %s" % (vname, vsrc))

    say("--- done ---")


if __name__ == "__main__":
    main()
