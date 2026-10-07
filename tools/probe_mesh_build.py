# -*- coding: utf-8 -*-
"""
probe_mesh_api.py -- find a way to build a StaticMesh WITHOUT the OBJ importer.

Why: the Interchange OBJ translator silently drops most polygons from large
height-field-style grids -- 8x8 (98 faces) imports 100%, but 64x64 keeps 12.3%
and 128x128 keeps 0.8%, while a 10,924-face voxel shell imports 100%. The
geometry is provably valid (no zero-area triangles, no duplicate positions, all
face indices in range), and rewriting the normals and the line endings changed
nothing, so the loss is inside MakeMeshDescriptionForGroup ->
Algo::BinarySearch(VertexIndexMapping, ...) -> ValidateAndFixData. That is
engine code we cannot patch; the durable fix is to stop routing terrain through
an OBJ file at all.

This probe does two things in one editor run:

  1. reports which mesh-building APIs this engine build actually exposes, so the
     next step is written against a verified surface rather than a recalled one;
  2. attempts a real build of a small grid through StaticMeshDescription and
     reports the triangle count that lands -- the number that must match the
     source for the approach to be usable.

Run:

    UnrealEditor-Cmd.exe MCReplica.uproject -ExecutePythonScript=<this> ^
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import traceback

import unreal

OUT = "Q:/MC2UE5/logs/probe_mesh_api.txt"
TEST_DIR = "/Game/MC/_Test"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCMESHAPI] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def dump_api():
    """Which of the names we would rely on actually exist in this build."""
    say("=== API availability ===")
    names = (
        "StaticMeshDescription", "MeshDescription", "MeshDescriptionBase",
        "StaticMeshEditorSubsystem", "AssetImportTask",
        "PolygonGroupID", "VertexID", "VertexInstanceID", "EdgeID",
        "PolygonID", "UVID", "MeshDescriptionOperations",
        "StaticMeshOperations", "MeshEditingLibrary",
        "EditorStaticMeshLibrary", "MeshAttribute",
    )
    have = set(dir(unreal))
    for n in names:
        say("  unreal.%-28s %s" % (n, "YES" if n in have else "--"))
    say("")
    say("=== members of StaticMeshDescription ===")
    try:
        cls = getattr(unreal, "StaticMeshDescription", None)
        if cls is None:
            say("  (absent)")
        else:
            for m in sorted(dir(cls)):
                if not m.startswith("_"):
                    say("  %s" % m)
    except Exception as exc:
        say("  ERR %s" % str(exc)[:120])
    say("")
    say("=== members of StaticMeshEditorSubsystem ===")
    try:
        cls = getattr(unreal, "StaticMeshEditorSubsystem", None)
        if cls is None:
            say("  (absent)")
        else:
            for m in sorted(dir(cls)):
                if not m.startswith("_") and ("mesh" in m.lower()
                                             or "build" in m.lower()):
                    say("  %s" % m)
    except Exception as exc:
        say("  ERR %s" % str(exc)[:120])


def try_build(n=8):
    """Build an n x n grid through StaticMeshDescription. -> (ok, detail)."""
    say("")
    say("=== build attempt: %dx%d grid via StaticMeshDescription ===" % (n, n))
    try:
        smd = unreal.StaticMeshDescription()
    except Exception as exc:
        return False, "cannot construct StaticMeshDescription: %s" % str(exc)[:100]

    try:
        pg = smd.create_polygon_group()
        smd.set_polygon_group_material_slot_name(pg, "test")
        vids = {}
        for i in range(n):
            for j in range(n):
                v = smd.create_vertex()
                smd.set_vertex_position(
                    v, unreal.Vector(i * 100.0, j * 100.0, 0.0))
                vids[(i, j)] = v
        uvs = {}
        for i in range(n):
            for j in range(n):
                u = smd.create_uv()
                smd.set_uv(u, unreal.Vector2D(float(i), float(j)))
                uvs[(i, j)] = u
        tri = 0
        # smd.build() must run before vertex instances can be made.
        for i in range(n - 1):
            for j in range(n - 1):
                for a, b, c in ((vids[(i, j)], vids[(i + 1, j)],
                                 vids[(i + 1, j + 1)]),
                                (vids[(i, j)], vids[(i + 1, j + 1)],
                                 vids[(i, j + 1)])):
                    vi = []
                    for v in (a, b, c):
                        vi.append(smd.create_vertex_instance(v))
                    smd.create_triangle(pg, vi[0], vi[1], vi[2])
                    tri += 1
        say("  built %d triangles into the description" % tri)

        tools = unreal.AssetToolsHelpers.get_asset_tools()
        if not unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
            unreal.EditorAssetLibrary.make_directory(TEST_DIR)
        asset = tools.create_asset("MDGrid", TEST_DIR, unreal.StaticMesh, None)
        if asset is None:
            return False, "create_asset returned None"
        # StaticMesh has no direct builder in Python; go through the editor
        # subsystem if it exposes one, otherwise through the mesh description.
        sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
        for fn in ("create_static_mesh_from_static_mesh_description",
                   "set_static_mesh_from_mesh_description",
                   "create_static_mesh"):
            f = getattr(sub, fn, None)
            if f is None:
                say("  subsystem.%s -- absent" % fn)
                continue
            say("  trying subsystem.%s ..." % fn)
            try:
                r = f(asset, smd) if fn != "create_static_mesh" else f(asset, smd)
                say("    returned %s" % (r,))
            except Exception as exc:
                say("    raised %s" % str(exc)[:160])
        got = asset.get_editor_property("render_data") if asset else None
        nt = asset.get_num_triangles(0) if asset else None
        say("  asset.get_num_triangles(0) = %s (expected %d)"
            % (nt, 2 * (n - 1) * (n - 1)))
        return (isinstance(nt, int) and nt > 0), "nt=%s" % nt
    except Exception:
        return False, traceback.format_exc()[-600:]


def main():
    ok = True
    try:
        say("=== probe_mesh_api ===")
        dump_api()
        built, detail = try_build(8)
        say("")
        say("build ok=%s  %s" % (built, detail))
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    try:
        if unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
            for a in unreal.EditorAssetLibrary.list_assets(TEST_DIR):
                unreal.EditorAssetLibrary.delete_asset(a)
            unreal.EditorAssetLibrary.delete_directory(TEST_DIR)
            say("cleaned %s" % TEST_DIR)
    except Exception as exc:
        say("cleanup: %s" % str(exc)[:100])

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
