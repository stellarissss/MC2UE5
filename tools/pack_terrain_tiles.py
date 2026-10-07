# -*- coding: utf-8 -*-
"""
pack_terrain_tiles.py -- turn the height field into a compact binary pack for
the editor side to build meshes from.

Why a pack instead of an OBJ. The editor's embedded Python has no numpy
("ModuleNotFoundError: No module named 'numpy'"), so the height field has to be
evaluated over here. And the OBJ route is out anyway: the Interchange OBJ
translator drops 99.2% of a 128x128 height field's polygons (measured; see
build_terrain_meshes.py for the full table). So this writes the mesh as plain
arrays the editor can read with the standard library alone -- no numpy on that
side, no OBJ parsing, no importer.

Per tile, under out/terrain_pack/:

    <name>.pos   float32  (nv, 3)  vertex positions, ALREADY in Unreal cm
    <name>.uv    float32  (nv, 2)  vertex UVs
    <name>.tri   int32    (nt, 3)  triangle -> vertex indices, 0-based
    <name>.fam   uint8    (nt,)    1-based family slot per triangle
    manifest.json                  tiles, family names, material paths

Positions are written the way the mesh builder needs them, which is the way the
OBJ importer would have delivered them: the writer's own (x, y, z) with Y
negated, and V flipped, matching UInterchangeOBJTranslator's PositionToUEBasis
and UVToUEBasis. Doing the basis change here rather than in the editor keeps the
editor side a dumb array reader.

Run with the project venv:

    Q:/MC2UE5/venv/Scripts/python.exe tools/pack_terrain_tiles.py
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_atlas import ATLAS_FAMILIES                          # noqa: E402
from extract_structures import SLOT_NAMES                      # noqa: E402
from terrain_smooth import (CAMPUS, BLOCK_CM, fill_invalid,     # noqa: E402
                            smooth_heightfield, surface_family_map)
from extract_structures import build_material_volume            # noqa: E402

PACK_DIR = "out/terrain_pack"
MAT_FMT = "/Game/MC/Families/M_MC_%s"
MESH_DIR = "/Game/MC/Terrain"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terrain", default="out/classify/terrain.npy")
    ap.add_argument("--bin", default="voxel_data/full/overworld.bin")
    ap.add_argument("--material-cache", default="out/materials/voxelmat.npz")
    ap.add_argument("--sigma", type=float, default=0.9)
    ap.add_argument("--flat-restore", type=float, default=0.85)
    ap.add_argument("--tile", type=int, default=128)
    ap.add_argument("--out", default=PACK_DIR)
    args = ap.parse_args()

    t0 = time.time()
    os.makedirs(args.out, exist_ok=True)

    h = np.load(args.terrain)
    h = fill_invalid(h)
    before = h.astype(np.float32)

    print("loading material volume for surface families ...")
    fam_vol, _nameid, _names = build_material_volume(args.bin,
                                                     args.material_cache)
    fam_map = surface_family_map(h, fam_vol)

    sm, shift = smooth_heightfield(before, args.sigma, args.flat_restore)
    sm = sm.astype(np.float32)
    print("smoothing: mean |dh| = %.3f m" % shift)
    print("surface families: %d distinct; other = %d columns"
          % (int(np.unique(fam_map).size),
             int((fam_map == ATLAS_FAMILIES.index("other") + 1).sum())))

    nu, nv = sm.shape
    x0, z0 = CAMPUS[0], CAMPUS[2]
    tiles = []
    tot_v = tot_t = 0
    for tu in range(0, nu, args.tile):
        for tv in range(0, nv, args.tile):
            bu, bv = min(tu + args.tile, nu), min(tv + args.tile, nv)
            if bu - tu < 2 or bv - tv < 2:
                continue
            name = "terrain_%04d_%04d" % (x0 + tu, z0 + tv)
            w, d = bu - tu, bv - tv
            fam = fam_map[tu:bu, tv:bv]

            # Vertices, row-major (i outer, j inner) -- the OBJ writer's order.
            ii, jj = np.meshgrid(np.arange(tu, bu), np.arange(tv, bv),
                                 indexing="ij")
            pos = np.empty((w * d, 3), dtype=np.float32)
            pos[:, 0] = (x0 + ii).ravel() * BLOCK_CM            # UE X
            pos[:, 1] = -(z0 + jj).ravel() * BLOCK_CM           # UE Y (negated)
            pos[:, 2] = sm[tu:bu, tv:bv].ravel() * BLOCK_CM     # UE Z (up)
            uv = np.empty((w * d, 2), dtype=np.float32)
            uv[:, 0] = ii.ravel() - tu                          # U
            uv[:, 1] = 1.0 - (jj.ravel() - tv)                  # V, flipped

            # Triangles per cell: (a,b,c) and (a,c,d) with a=(i,j),
            # b=(i+1,j), c=(i+1,j+1), d=(i,j+1) -- the OBJ writer's winding.
            gi, gj = np.meshgrid(np.arange(w - 1), np.arange(d - 1),
                                 indexing="ij")
            a = (gi * d + gj).ravel()
            b = ((gi + 1) * d + gj).ravel()
            c = ((gi + 1) * d + (gj + 1)).ravel()
            dd = (gi * d + (gj + 1)).ravel()
            tri = np.empty((2 * a.size, 3), dtype=np.int32)
            tri[0::2, 0], tri[0::2, 1], tri[0::2, 2] = a, b, c
            tri[1::2, 0], tri[1::2, 1], tri[1::2, 2] = a, c, dd
            facefam = np.repeat(fam[:w - 1, :d - 1].ravel(), 2).astype(np.uint8)

            keep = facefam > 0
            if not keep.any():
                print("  %-22s skipped (no family)" % name)
                continue
            tri, facefam = tri[keep], facefam[keep]

            for suffix, arr in (("pos", pos), ("uv", uv), ("tri", tri),
                                ("fam", facefam)):
                arr.tofile(os.path.join(args.out, "%s.%s" % (name, suffix)))

            fams = sorted(int(s) for s in np.unique(facefam))
            tiles.append({
                "name": name,
                "mesh_path": "%s/%s" % (MESH_DIR, name),
                "vertex_count": int(pos.shape[0]),
                "triangle_count": int(tri.shape[0]),
                # `.fam` holds the ABSOLUTE SLOT_NAMES slot (1-based), while
                # `slots` lists only the families this tile actually uses. The
                # two are paired by position so the builder can map one to the
                # other without re-deriving the family table.
                "slot_numbers": fams,
                "slots": [SLOT_NAMES[s - 1] for s in fams],
                "materials": [MAT_FMT % SLOT_NAMES[s - 1] for s in fams],
                "origin_block": [x0 + tu, z0 + tv],
                "size_blocks": [w, d],
            })
            tot_v += pos.shape[0]
            tot_t += tri.shape[0]

    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump({
            "campus": list(CAMPUS),
            "coordinate_note":
                "positions are already in Unreal cm, with the OBJ importer's "
                "basis change applied: OBJ (x,y,z) -> UE (x,-y,z), UV (u,v) -> "
                "(u,1-v). Building from this pack therefore produces the same "
                "surface the OBJ path would have, without the importer.",
            "why_pack":
                "the editor's embedded Python has no numpy and the Interchange "
                "OBJ translator drops 99.2% of a 128x128 height field",
            "tile_px": args.tile,
            "source": {"terrain": args.terrain, "bin": args.bin,
                       "sigma": args.sigma, "flat_restore": args.flat_restore},
            "totals": {"tiles": len(tiles), "vertices": tot_v,
                       "triangles": tot_t},
            "tiles": tiles,
        }, fh, indent=2)

    print("wrote %d tiles -> %s" % (len(tiles), args.out))
    print("  %s vertices, %s triangles  (%.1fs)"
          % (format(tot_v, ","), format(tot_t, ","), time.time() - t0))
    print("  128x128 tile triangle check: %s"
          % sorted({t["triangle_count"] for t in tiles
                    if t["size_blocks"] == [128, 128]}))


if __name__ == "__main__":
    main()
