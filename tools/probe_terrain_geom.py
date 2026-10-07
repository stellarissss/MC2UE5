# -*- coding: utf-8 -*-
"""
probe_terrain_geom.py -- is the terrain mesh's geometry actually 9.5% of the OBJ,
or is get_num_triangles(0) reporting something else (Nanite fallback, a coarse
LOD)?

probe_sections.py printed `mesh.get_num_triangles(0) = 3051` for a tile whose OBJ
has 32258 triangles, while bld_001_structure matched its OBJ exactly. Either the
terrain import dropped 90% of the geometry (the failure mode
import_chunks.py's docstring warns about) or the number means something else.
Those have opposite fixes, so measure rather than guess.

Read-only. Run:
  UnrealEditor-Cmd.exe MCReplica.uproject \
      -ExecutePythonScript=tools/probe_terrain_geom.py -nullrhi -unattended -nopause
"""

import os

import unreal

OUT = "Q:/MC2UE5/logs/probe_terrain_geom.txt"
TERRAIN = "Q:/MC2UE5/repo/out/terrain"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCGEOM] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def obj_tris(path):
    t = 0
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if line.startswith("f "):
                k = len(line.split()) - 1
                t += (k - 2) if k >= 3 else 0
    return t


def main():
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    try:
        lib = unreal.EditorStaticMeshLibrary
        lib_names = sorted(n for n in dir(lib) if not n.startswith("_"))
        say("EditorStaticMeshLibrary members: %s" % lib_names)
    except Exception as exc:
        lib = None
        say("no EditorStaticMeshLibrary: %s" % str(exc)[:80])

    probes = [
        ("terrain_-144_-544", "/Game/MC/Terrain/terrain_-144_-544",
         os.path.join(TERRAIN, "terrain_-144_-544.obj")),
        ("terrain_-144_-416", "/Game/MC/Terrain/terrain_-144_-416",
         os.path.join(TERRAIN, "terrain_-144_-416.obj")),
        ("terrain_-016_-032", "/Game/MC/Terrain/terrain_-016_-032",
         os.path.join(TERRAIN, "terrain_-016_-032.obj")),
        ("bld_001_structure", "/Game/MC/Structures/bld_001_structure",
         "Q:/MC2UE5/repo/out/structures/bld_001_structure.obj"),
    ]

    for name, path, obj in probes:
        m = unreal.load_asset(path)
        say("")
        say("=== %s ===" % name)
        if m is None:
            say("  MISSING")
            continue
        want = obj_tris(obj)
        say("  OBJ triangles: %d" % want)

        try:
            nl = m.get_num_lods()
            say("  get_num_lods() -> %d" % nl)
        except Exception as exc:
            nl = None
            say("  get_num_lods raised %s" % str(exc)[:70])

        if nl:
            for lod in range(nl):
                try:
                    t = m.get_num_triangles(lod)
                    say("  get_num_triangles(%d) -> %d  (%.1f%% of OBJ)"
                        % (lod, t, 100.0 * t / want if want else 0))
                except Exception as exc:
                    say("  get_num_triangles(%d) raised %s" % (lod, str(exc)[:60]))

        try:
            v = m.get_num_vertices(0)
            say("  mesh.get_num_vertices(0) -> %s" % v)
        except Exception as exc:
            say("  mesh.get_num_vertices(0) raised %s" % str(exc)[:70])

        if lib is not None:
            for fn_name in ("get_number_triangles", "get_number_verts",
                            "get_lod_count"):
                f = getattr(lib, fn_name, None)
                if f is None:
                    continue
                for args in ((m, 0), (m,)):
                    try:
                        say("  EditorStaticMeshLibrary.%s%s -> %r"
                            % (fn_name, args[1:] or "", f(*args)))
                        break
                    except Exception as exc:
                        last = str(exc)[:60]
                else:
                    say("  EditorStaticMeshLibrary.%s raised %s"
                        % (fn_name, last))

        # Nanite: if on, get_num_triangles may be reporting the fallback mesh.
        try:
            st = sub.get_nanite_settings(m)
            say("  nanite enabled = %s" % st.get_editor_property("enabled"))
        except Exception as exc:
            say("  nanite settings raised %s" % str(exc)[:70])
        try:
            say("  nanite_triangle_percent = %s"
                % st.get_editor_property("triangle_percent"))
        except Exception:
            pass
        # Build settings that decide whether Nanite is even allowed
        for prop in ("nanite_settings", "lod_group", "num_lods"):
            try:
                say("  prop %s = %r" % (prop, m.get_editor_property(prop)))
            except Exception:
                pass

    say("--- done ---")


main()
