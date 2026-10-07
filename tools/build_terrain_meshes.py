# -*- coding: utf-8 -*-
"""
build_terrain_meshes.py -- build the smooth terrain tiles as StaticMesh assets
from the binary pack, with no OBJ file and no importer in the path.

The problem this exists to solve
--------------------------------
The Interchange OBJ translator silently drops almost every polygon from a large
height field. Every row below is an actual import on this engine (UE 5.8):

    faces in the OBJ          triangles that landed
      98  (8x8 flat grid)       98    100%    <- a small grid is fine
    7,938 (64x64 flat grid)    975    12.3%
   32,258 (128x128 flat tile)  252     0.8%
   32,258 (128x128 hilly tile) 3,051   9.5%
   10,924 (voxel shell)      10,924  100%    <- shells are fine

Three explanations were tested and refuted rather than argued away: rewriting
every "-0.0000" normal as "+0.0000" changed nothing; rewriting the file to LF-only
changed nothing; and a 1,000-face file still landed at 255 triangles, so the loss
is neither proportional nor about signed zeros or line endings. The importer logs
no warning at all, which means it believes it succeeded. The drop is inside
UInterchangeOBJTranslator::MakeMeshDescriptionForGroup
(InterchangeOBJTranslator.cpp) -- engine code we cannot patch here. Writing quads
instead of triangles is worse: that build ran for minutes and ended with
"Input has 0 triangles".

What is used instead
--------------------
    StaticMesh.create_static_mesh_description()
    StaticMeshDescription.create_vertex / create_vertex_instance /
        create_triangle / create_polygon_group / set_vertex_position /
        set_vertex_instance_uv / set_polygon_group_material_slot_name
    StaticMesh.build_from_static_mesh_descriptions([smd])

Measured on this engine: a 128x128 flat grid lands at 32,258 / 32,258 triangles
in 0.7 s.

Why the geometry comes from a pack
----------------------------------
The editor's embedded Python has **no numpy** ("ModuleNotFoundError: No module
named 'numpy'"), so the height field cannot be evaluated here. tools/
pack_terrain_tiles.py evaluates it in the project venv and writes plain arrays;
this side reads them with the standard library only. No numpy, no OBJ parsing,
no importer -- and the basis change is already applied in the pack, so what lands
here is exactly what the OBJ path would have produced.

Asset names are unchanged, so the 24 terrain actors already in the level pick up
the rebuilt meshes without the level being touched.

Run inside the editor (argv is not forwarded, so options come from env vars):

    set MC_TILE_FILTER=terrain_-016_-032     # optional: build just one tile
    UnrealEditor-Cmd.exe MCReplica.uproject -ExecutePythonScript=<this> ^
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import array
import json
import os
import time
import traceback

import unreal

REPO = "Q:/MC2UE5/repo"
PACK = os.path.join(REPO, "out/terrain_pack")
OUT = "Q:/MC2UE5/logs/build_terrain_meshes.txt"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCTERBUILD] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def read_array(path, typecode, per_item):
    """Read a flat binary file into `per_item`-sized tuples. Stdlib only.

    The editor Python has no numpy, so this is the whole reader: array.array
    handles the byte order, and the data is grouped here.
    """
    a = array.array(typecode)
    with open(path, "rb") as fh:
        a.frombytes(fh.read())
    if len(a) % per_item:
        raise ValueError("%s: %d values is not a multiple of %d"
                         % (path, len(a), per_item))
    n = len(a) // per_item
    return [tuple(a[i * per_item:(i + 1) * per_item]) for i in range(n)]


def probes():
    """Report the stdlib surface, so a missing module is a message not a crash."""
    say("=== editor python ===")
    say("  array  OK   json OK")
    try:
        import struct as _s            # noqa: F401
        say("  struct OK")
    except Exception as exc:
        say("  struct MISSING (%s)" % str(exc)[:60])
    try:
        import numpy as _n             # noqa: F401
        say("  numpy  present (unexpected)")
    except Exception:
        say("  numpy  ABSENT -- which is why the geometry comes from a pack")


def build_tile(t):
    """Create/refresh one StaticMesh asset from its packed arrays."""
    name = t["name"]
    pos = read_array(os.path.join(PACK, name + ".pos"), "f", 3)
    uv = read_array(os.path.join(PACK, name + ".uv"), "f", 2)
    tri = read_array(os.path.join(PACK, name + ".tri"), "i", 3)
    fam = array.array("B")
    with open(os.path.join(PACK, name + ".fam"), "rb") as fh:
        fam.frombytes(fh.read())

    want = len(tri)
    # `.fam` carries the absolute ATLAS_FAMILIES slot; `slots` lists only the
    # families present in this tile. Pair them by position to get the name.
    slot_numbers = list(t["slot_numbers"])
    slots = list(t["slots"])
    name_of_slot = dict(zip(slot_numbers, slots))
    if len(fam) != want:
        return {"name": name, "error": "fam %d != tri %d" % (len(fam), want)}

    asset = unreal.load_asset(t["mesh_path"])
    if asset is None:
        asset = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            name, "/Game/MC/Terrain", unreal.StaticMesh, None)
    if asset is None:
        return {"name": name, "error": "could not create/load asset"}

    smd = asset.create_static_mesh_description()
    if smd is None:
        return {"name": name, "error": "create_static_mesh_description() -> None"}

    # Polygon groups in the manifest's slot order, so group order, slot-name
    # order and the material array order all agree.
    group_of = {}
    for nm in slots:
        g = smd.create_polygon_group()
        smd.set_polygon_group_material_slot_name(g, nm)
        group_of[nm] = g

    smd.reserve_new_vertices(len(pos))
    vids = []
    for p in pos:
        v = smd.create_vertex()
        smd.set_vertex_position(v, unreal.Vector(float(p[0]), float(p[1]),
                                                 float(p[2])))
        vids.append(v)

    # A fresh vertex instance per triangle corner, deliberately.
    #
    # Sharing vertex instances is the topology a normal mesh would use, and two
    # attempts at it both crashed the async build with
    # "Assertion failed: (Index >= 0) & (Index < ArrayNum) [Array.h:1339]" in a
    # background worker -- once when instances were shared per vertex, once when
    # they were shared per (polygon group, vertex), and it still crashed with
    # reserve_new_triangles removed. Whatever the binding objects to, a fresh
    # instance per corner avoids it entirely and the triangle counts come out
    # exactly right. The cost is that the mesh is effectively non-indexed
    # (3 instances per triangle instead of ~1 per vertex), which is a known
    # optimisation to revisit -- correctness first.
    for k in range(want):
        i0, i1, i2 = tri[k]
        nm = name_of_slot[fam[k]]
        iv = []
        for idx in (i0, i1, i2):
            inst = smd.create_vertex_instance(vids[idx])
            smd.set_vertex_instance_uv(
                inst, unreal.Vector2D(float(uv[idx][0]), float(uv[idx][1])), 0)
            iv.append(inst)
        smd.create_triangle(group_of[nm], iv)

    # Materials are set BEFORE the build, not after.
    #
    # build_from_static_mesh_descriptions creates one section per polygon group
    # and resolves each section's material through the asset's static_materials
    # array, in group order. An asset created here starts with an empty (or
    # short) array, and a build against it dies with
    # "Assertion failed: (Index >= 0) & (Index < ArrayNum) [Array.h:1339]" in a
    # background worker -- which is exactly why the first build of a fresh tile
    # failed while rebuilding a tile that had already been through the OBJ
    # importer (and therefore already had materials) sometimes got through.
    entries = []
    for nm, mat_path in zip(slots, t["materials"]):
        m = unreal.load_asset(mat_path)
        if m is None:
            return {"name": name, "error": "missing material %s" % mat_path}
        e = unreal.StaticMaterial()
        e.set_editor_property("material_interface", m)
        try:
            e.set_editor_property("material_slot_name", nm)
        except Exception:
            pass
        entries.append(e)
    asset.set_editor_property("static_materials", entries)

    # fast_build=False: with the default (True) the async static mesh build
    # raced the save and crashed the background worker with
    # "Assertion failed: (Index >= 0) & (Index < ArrayNum) [Array.h:1339]".
    # Overridable so the choice can be revisited rather than hard-coded.
    asset.build_from_static_mesh_descriptions(
        [smd], build_simple_collision=False,
        fast_build=os.environ.get("MC_FAST_BUILD", "0") == "1")

    unreal.EditorAssetLibrary.save_loaded_asset(asset)

    return {"name": name, "want": want, "got": asset.get_num_triangles(0),
            "sections": asset.get_num_sections(0), "slots": slots,
            "verts": asset.get_num_vertices(0)}


def main():
    ok = True
    t0 = time.time()
    try:
        say("=== build_terrain_meshes (from pack, no OBJ, no importer) ===")
        probes()
        man_path = os.path.join(PACK, "manifest.json")
        if not os.path.isfile(man_path):
            say("*** no pack at %s -- run tools/pack_terrain_tiles.py first"
                % PACK)
            raise SystemExit(1)
        with open(man_path) as fh:
            man = json.load(fh)
        tiles = man["tiles"]
        filt = os.environ.get("MC_TILE_FILTER", "").strip()
        if filt:
            tiles = [t for t in tiles if t["name"] == filt]
        # Batching exists because the async static mesh build crashes when many
        # heavy tiles are built back to back in one process. Each tile is
        # written in place and the whole script is idempotent, so running it in
        # slices is safe and resumable; MC_BATCH=N of MC_BATCHES=M takes slice N.
        nbatches = max(1, int(os.environ.get("MC_BATCHES", "1") or 1))
        ibatch = int(os.environ.get("MC_BATCH", "0") or 0)
        if nbatches > 1:
            per = (len(tiles) + nbatches - 1) // nbatches
            lo, hi = ibatch * per, min(len(tiles), (ibatch + 1) * per)
            tiles = tiles[lo:hi]
            say("batch %d/%d -> tiles[%d:%d]" % (ibatch, nbatches, lo, hi))
        say("pack: %d tiles, %s vertices, %s triangles"
            % (man["totals"]["tiles"], format(man["totals"]["vertices"], ","),
               format(man["totals"]["triangles"], ",")))
        say("building %d tile(s)%s" % (len(tiles), " (filtered)" if filt else ""))

        want_total = got_total = 0
        bad = []
        for t in tiles:
            st = build_tile(t)
            if "error" in st:
                bad.append(st)
                say("  %-22s *** %s" % (st["name"], st["error"]))
                continue
            want_total += st["want"]
            got_total += st["got"] if isinstance(st["got"], int) else 0
            flag = "" if st["got"] == st["want"] else "   *** MISMATCH"
            say("  %-22s tris %6d / %-6d  verts %-7s sections %-2s slots %s%s"
                % (st["name"], st["got"], st["want"], st["verts"],
                   st["sections"], ",".join(st["slots"]), flag))
            if st["got"] != st["want"]:
                bad.append(st)

        say("")
        say("totals: triangles %d / %d" % (got_total, want_total))
        if bad:
            ok = False
            say("FAILED: %s" % ", ".join(b["name"] for b in bad))
        else:
            say("every tile matched its packed triangle count")
        say("elapsed %.1fs" % (time.time() - t0))
    except SystemExit:
        ok = False
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
