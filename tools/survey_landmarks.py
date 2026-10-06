# -*- coding: utf-8 -*-
"""
survey_landmarks.py -- locate the campus landmarks in the voxel data.

The core constraint of this project is that the UE campus corresponds to the
Minecraft map: the sports field, the gate and each building have to be where the
save says they are. That makes "where is the sports field" an input to framing
and to verification, not something to eyeball from a render.

Landmarks are found by the materials people actually build them from, which is
how a human would find them in game:

  * **sports field** -- dyed wool. Fields, tracks and courts in a Minecraft
    campus are wool; a colour cluster of wool at ground level is a field.
  * **buildings** -- the light structural palette (white / light-grey concrete,
    quartz, smooth sandstone). Connected into blobs by a coarse 8 m grid so one
    school block does not read as hundreds of separate walls.
  * **gate / entrance** -- iron bars and fences, which is what a campus gate is
    made of, clustered near the campus boundary.

Output is a JSON report of centroids, extents and heights, which is what a
camera placement actually needs.

    python3 tools/survey_landmarks.py --bin voxel_data/full/overworld.bin
"""

import argparse
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "phase2"))
from voxelio import VoxelFile                     # noqa: E402

CAMPUS = (-144, 303, -544, 223)
BLOCK_CM = 100.0

#: Palette groups by landmark role.
FIELD_MATS = ("lime_wool", "green_wool", "red_wool", "white_wool", "cyan_wool",
              "blue_wool", "yellow_wool", "orange_wool", "black_wool")
BUILDING_MATS = ("white_concrete", "light_gray_concrete", "quartz_block",
                 "smooth_quartz", "smooth_sandstone", "white_terracotta")
GATE_MATS = ("iron_bars", "birch_fence", "oak_fence", "iron_door",
             "birch_door", "oak_door", "dark_oak_fence", "spruce_fence")

#: Blob grid, in blocks. Coarse enough that one building is one blob.
BLOB = 8


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, dest="bin_path")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    x0, x1, z0, z1 = CAMPUS
    W, D = x1 - x0 + 1, z1 - z0 + 1

    # Per material group: point cloud of world coordinates.
    pts = defaultdict(list)
    with VoxelFile(args.bin_path) as vf:
        for ch in vf.iter_chunks():
            cx, cz = ch["chunkX"], ch["chunkZ"]
            if (cx * 16 > x1 or cx * 16 + 15 < x0
                    or cz * 16 > z1 or cz * 16 + 15 < z0):
                continue
            if ch["state"].size == 0:
                continue
            xyz, st = ch["xyz"], ch["state"]
            for s in np.unique(st):
                nm = vf.palette[int(s)][0].split(":", 1)[-1]
                if nm in FIELD_MATS:
                    g = "field"
                elif nm in BUILDING_MATS:
                    g = "building"
                elif nm in GATE_MATS:
                    g = "gate"
                else:
                    continue
                m = st == s
                pts[g].append((xyz[m, 0], xyz[m, 1], xyz[m, 2],
                               np.full(int(m.sum()), nm, dtype=object)))

    report = {"campus": list(CAMPUS), "groups": {}}

    for g, chunks in pts.items():
        xs = np.concatenate([c[0] for c in chunks])
        ys = np.concatenate([c[1] for c in chunks])
        zs = np.concatenate([c[2] for c in chunks])
        names = np.concatenate([c[3] for c in chunks])
        report["groups"][g] = {
            "n": int(xs.size),
            "centroid": [int(xs.mean()), int(ys.mean()), int(zs.mean())],
            "x": [int(xs.min()), int(xs.max())],
            "y": [int(ys.min()), int(ys.max())],
            "z": [int(zs.min()), int(zs.max())],
        }
        # Blob the group on a coarse grid and keep the biggest blobs, so a
        # cluster of buildings reports as separate buildings.
        bx = (xs - x0) // BLOB
        bz = (zs - z0) // BLOB
        key = bx * 10000 + bz
        uniq, counts = np.unique(key, return_counts=True)
        order = np.argsort(-counts)[:8]
        blobs = []
        for i in order:
            k = uniq[i]
            m = key == k
            blobs.append({
                "n": int(counts[i]),
                "block_x": int(xs[m].mean()),
                "block_y": int(ys[m].mean()),
                "block_z": int(zs[m].mean()),
                "top_y": int(ys[m].max()),
                "span_blocks": [int(xs[m].max() - xs[m].min()),
                                int(zs[m].max() - zs[m].min())],
            })
        report["groups"][g]["blobs"] = blobs

        # For a field, the colour mix says which kind of field it is.
        if g == "field":
            u, c = np.unique(names, return_counts=True)
            report["groups"][g]["colours"] = {
                str(n): int(k) for n, k in
                sorted(zip(u, c), key=lambda t: -t[1])[:6]}

    print(json.dumps(report, indent=2))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
        print("report: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
