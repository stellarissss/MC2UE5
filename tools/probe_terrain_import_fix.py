# -*- coding: utf-8 -*-
"""
probe_terrain_import_fix.py -- one run, four candidate answers.

Established so far, all by direct test rather than argument:

  * the harness is sound -- a 10,924-face voxel shell imports at 100%, and the
    terrain numbers this reports match the shipped assets exactly;
  * negative-zero normals are not the cause (a rewritten variant imports
    identically);
  * CRLF line endings are not the cause (an LF-only rewrite imports identically);
  * the loss is not proportional: a 1,000-face file and a 32,258-face file both
    land at ~255 triangles, while an 8x8 grid (98 faces) imports whole;
  * the importer logs no warning at all, so it believes it succeeded. The drop
    happens inside UInterchangeOBJTranslator::MakeMeshDescriptionForGroup
    (InterchangeOBJTranslator.cpp), which is engine code we cannot patch.

So the question is no longer "what is broken" but "which route around it works".
This run tests three, plus dumps the API surface for a fourth:

  quads    the same 128x128 tile written as 16,129 quads instead of 32,258
           triangles -- n-gons take a different path through the translator.
  groups   the same tile with a usemtl every 16 rows, giving 8 face groups
           instead of 1. The voxel shells that DO import whole all have many
           material groups (bld_028 has 362 sections), so this tests whether
           group count is what saves them.
  flatN    flat grids at 16, 32 and 48 across, to bisect where a flat grid
           starts collapsing (8 imports whole, 64 keeps 12%).
  api      what StaticMesh / EditorStaticMeshLibrary expose, for the route that
           bypasses the importer entirely by building a MeshDescription.

Run:

    UnrealEditor-Cmd.exe MCReplica.uproject -ExecutePythonScript=<this> ^
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import os
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/probe_terrain_import_fix.txt"
REPO = "Q:/MC2UE5/repo"
TEST_DIR = "/Game/MC/_Test"
L = []

CASES = (
    ("junk_quads", os.path.join(REPO, "out/_fix_quads.obj"), 16129),
    ("junk_groups", os.path.join(REPO, "out/_fix_groups.obj"), 32258),
    ("junk_flat16", os.path.join(REPO, "out/_t_flat16.obj"), 450),
    ("junk_flat32", os.path.join(REPO, "out/_t_flat32.obj"), 1922),
    ("junk_flat48", os.path.join(REPO, "out/_t_flat48.obj"), 4418),
)


def say(s):
    L.append(str(s))
    unreal.log("[MCTERFIX] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def count_faces(path):
    n = 0
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("f "):
                    n += 1
    except OSError:
        return -1
    return n


def dump_api():
    say("=== api: what the no-importer route would need ===")
    have = set(dir(unreal))
    for n in ("StaticMeshFactory", "StaticMeshDescription", "MeshDescriptionBase",
              "EditorStaticMeshLibrary", "StaticMeshEditorSubsystem",
              "MeshDescription", "StaticMeshOperations", "MeshEditingLibrary"):
        say("  unreal.%-28s %s" % (n, "YES" if n in have else "--"))
    say("")
    say("  --- unreal.StaticMesh, build/mesh related ---")
    try:
        for m in sorted(dir(unreal.StaticMesh)):
            if m.startswith("_"):
                continue
            low = m.lower()
            if any(k in low for k in ("mesh", "build", "description", "lod",
                                      "material", "section")):
                say("    %s" % m)
    except Exception as exc:
        say("    ERR %s" % str(exc)[:100])
    say("")
    say("  --- unreal.EditorStaticMeshLibrary, all ---")
    try:
        for m in sorted(dir(unreal.EditorStaticMeshLibrary)):
            if not m.startswith("_"):
                say("    %s" % m)
    except Exception as exc:
        say("    ERR %s" % str(exc)[:100])


def main():
    ok = True
    try:
        say("=== probe_terrain_import_fix ===")
        dump_api()
        say("")
        say("=== import candidates ===")
        if not unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
            unreal.EditorAssetLibrary.make_directory(TEST_DIR)

        results = []
        for label, src, expected in CASES:
            src = src.replace("\\", "/")
            if not os.path.isfile(src):
                say("  %-14s *** MISSING %s" % (label, src))
                ok = False
                continue
            nf = count_faces(src)
            task = unreal.AssetImportTask()
            task.set_editor_property("filename", src)
            task.set_editor_property("destination_path", TEST_DIR)
            task.set_editor_property("destination_name", label)
            task.set_editor_property("automated", True)
            task.set_editor_property("replace_existing", True)
            task.set_editor_property("save", False)
            unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
            mesh = unreal.load_asset("%s/%s" % (TEST_DIR, label))
            if mesh is None:
                say("  %-14s *** produced nothing" % label)
                ok = False
                continue
            try:
                tri = mesh.get_num_triangles(0)
                verts = mesh.get_num_vertices(0)
                secs = mesh.get_num_sections(0)
            except Exception as exc:
                say("  %-14s *** stats failed: %s" % (label, str(exc)[:80]))
                ok = False
                continue
            pct = 100.0 * tri / nf if nf else 0.0
            say("  %-14s f=%-6d -> tris=%-7s verts=%-7s sections=%-4s  %.1f%%"
                % (label, nf, tri, verts, secs, pct))
            results.append((label, nf, tri, pct, secs))

        say("")
        say("=== known references ===")
        say("  8x8 flat grid, 98 faces        -> 100%% (imports whole)")
        say("  64x64 flat grid, 7938 faces    -> 12.3%%")
        say("  128x128 flat tile, 32258 faces -> 0.8%%")
        say("  128x128 terrain tile, 32258    -> 9.5%%")
        say("  10,924-face voxel shell        -> 100%% (362 sections)")
        say("")
        say("=== verdict ===")
        d = {r[0]: r for r in results}
        q = d.get("junk_quads")
        g = d.get("junk_groups")
        if q:
            say("  quads : %s/%d = %.1f%%  %s"
                % (q[2], q[1], q[3],
                   "WORKS" if q[3] > 99 else "no better than triangles"))
        if g:
            say("  groups: %s/%d = %.1f%%  %s"
                % (g[2], g[1], g[3],
                   "WORKS" if g[3] > 99 else "group count is not the factor"))
        for n in (16, 32, 48):
            r = d.get("junk_flat%d" % n)
            if r:
                say("  flat%-3d: %s/%d = %.1f%%" % (n, r[2], r[1], r[3]))
        if q and q[3] > 99:
            say("")
            say("  => FIX AVAILABLE: write quads instead of triangles. That is a")
            say("     one-line change in terrain_smooth.build_tiles and needs no")
            say("     new API and no new dependency.")
        elif g and g[3] > 99:
            say("")
            say("  => FIX AVAILABLE: group the faces (usemtl every N rows). Also a")
            say("     small change confined to terrain_smooth.build_tiles.")
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    try:
        for label, _s, _e in CASES:
            p = "%s/%s" % (TEST_DIR, label)
            if unreal.EditorAssetLibrary.does_asset_exist(p):
                unreal.EditorAssetLibrary.delete_asset(p)
        if unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
            unreal.EditorAssetLibrary.delete_directory(TEST_DIR)
            say("")
            say("cleaned %s" % TEST_DIR)
    except Exception as exc:
        say("cleanup: %s" % str(exc)[:100])

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
