# -*- coding: utf-8 -*-
"""
pick_spawn.py -- choose the player start from the voxel data.

Written because the spawn was being picked by hand and kept being wrong: the
last hand-picked point, the centroid of the sports field, turned out to be
**inside a birch tree** -- leaves filled y=11..20 above the camera, the first
frame was the inside of a canopy, and the whole campus looked missing. A
centroid is the average of a set of wool blocks, and averaging a field does not
tell you what is standing on it.

So the start is now *searched* rather than chosen, against criteria that come
from what a player needs at t=0:

  * **standing on ground** -- a solid top between y=2 and y=12, so the spawn is
    on the campus surface and not on a roof;
  * **headroom** -- no solid block in the 3 blocks above the feet, and no solid
    block in the 2 columns to each side, which is what excludes being inside a
    tree, a doorway, or a wall;
  * **sky above** -- nothing solid in the whole column above, so the spot is
    outdoors;
  * **on or beside the field** -- wool within 16 blocks, so the opening view is
    the sports field rather than a service road;
  * **a clear sightline** -- a straight run of 30 blocks toward the building
    mass at head height, sampled every 2 blocks, so the first frame actually
    contains the campus instead of the back of a shed.

Output is JSON, consumed by project/Content/Python/apply_spawn.py.

    python3 tools/pick_spawn.py --bin voxel_data/full/overworld.bin
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "phase2"))
from voxelio import VoxelFile                     # noqa: E402

CAMPUS = (-144, 303, -544, 223)
BLOCK_CM = 100.0

WOOL_SUFFIX = "wool"
BUILDING_NAMES = ("white_concrete", "light_gray_concrete", "quartz_block",
                  "smooth_quartz", "smooth_sandstone")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, dest="bin_path")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    x0, x1, z0, z1 = CAMPUS
    W, D = x1 - x0 + 1, z1 - z0 + 1
    H = 72                       # campus tops out at y=62

    solid = np.zeros((W, H, D), dtype=bool)
    wool = np.zeros((W, D), dtype=bool)

    with VoxelFile(args.bin_path) as vf:
        for ch in vf.iter_chunks():
            cx, cz = ch["chunkX"], ch["chunkZ"]
            if (cx * 16 > x1 or cx * 16 + 15 < x0
                    or cz * 16 > z1 or cz * 16 + 15 < z0):
                continue
            if ch["state"].size == 0:
                continue
            xyz, st = ch["xyz"], ch["state"]
            ix = xyz[:, 0] - x0
            iz = xyz[:, 2] - z0
            iy = xyz[:, 1]
            keep = (ix >= 0) & (ix < W) & (iz >= 0) & (iz < D) & (iy < H)
            if not keep.any():
                continue
            ix, iz, iy, st = ix[keep], iz[keep], iy[keep], st[keep]
            solid[ix, iy, iz] = True
            for s in np.unique(st):
                nm = vf.palette[int(s)][0].split(":", 1)[-1]
                if nm.endswith(WOOL_SUFFIX):
                    m = st == s
                    wool[ix[m], iz[m]] = True

    # Highest solid block per column, and whether the column is open above it.
    top = np.full((W, D), -1, dtype=np.int16)
    for y in range(H):
        top = np.where(solid[:, y, :], y, top)

    # Building mass, for the sightline target.
    bx = bz = bn = 0
    bs = bsx = bsz = 0
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
                if nm in BUILDING_NAMES:
                    m = st == s
                    bsx += float(xyz[m, 0].sum())
                    bsz += float(xyz[m, 2].sum())
                    bn += int(m.sum())
    build_x = bsx / bn if bn else 0.0
    build_z = bsz / bn if bn else 0.0

    best = None
    reasons = {"ground": 0, "headroom": 0, "sky": 0, "field": 0, "sight": 0}
    for ix in range(2, W - 2):
        for iz in range(2, D - 2):
            t = int(top[ix, iz])
            if t < 2 or t > 12:
                continue
            reasons["ground"] += 1
            # Headroom: 3 above the feet, plus a 2-block skirt.
            if solid[ix, t + 1:min(t + 4, H), iz].any():
                continue
            if solid[ix - 2:ix + 3, t + 1:min(t + 3, H), iz - 2:iz + 3].any():
                continue
            reasons["headroom"] += 1
            # Outdoors.
            if solid[ix, min(t + 4, H):, iz].any():
                continue
            reasons["sky"] += 1
            # Beside the field.
            w0x, w1x = max(0, ix - 16), min(W, ix + 17)
            w0z, w1z = max(0, iz - 16), min(D, iz + 17)
            if not wool[w0x:w1x, w0z:w1z].any():
                continue
            reasons["field"] += 1

            # Sightline toward the buildings at head height.
            px, pz = x0 + ix, z0 + iz
            dx, dz = build_x - px, build_z - pz
            n = max((dx * dx + dz * dz) ** 0.5, 1.0)
            ux, uz = dx / n, dz / n
            clear = True
            for step in range(2, 32, 2):
                sx = int(round(px + ux * step)) - x0
                sz = int(round(pz + uz * step)) - z0
                if not (0 <= sx < W and 0 <= sz < D):
                    break
                st_ = int(top[sx, sz])
                # Anything taller than head height blocks the view.
                if st_ > t + 2:
                    clear = False
                    break
            if not clear:
                continue
            reasons["sight"] += 1

            # Prefer: standing low, close to wool, far enough to see a building.
            dist_to_build = n
            score = (-abs(t - 4) * 2.0
                     - abs(dist_to_build - 140.0) * 0.05
                     - abs(t - 4))
            if best is None or score > best[0]:
                best = (score, px, pz, t)

    if best is None:
        print("no column satisfied the criteria; funnel was %s" % reasons)
        return 1

    _, px, pz, py = best
    dx, dz = build_x - px, build_z - pz
    # Minecraft +z maps to Unreal +Y, and Unreal yaw 0 looks along +X, so the
    # heading from (dx, dz) is atan2(dz, dx). This is the one axis conversion in
    # the whole chain, so it is derived here rather than typed into a table.
    yaw = float(np.degrees(np.arctan2(dz, dx)))

    info = {
        "spawn_block": [int(px), int(pz)],
        "surface_block": int(py),
        "spawn_cm": [px * BLOCK_CM, pz * BLOCK_CM,
                     (py + 1) * BLOCK_CM + 100.0],
        "yaw": round(yaw, 1),
        "building_core_block": [round(build_x), round(build_z)],
        "distance_to_buildings_blocks": round(
            float(((build_x - px) ** 2 + (build_z - pz) ** 2) ** 0.5), 1),
        "funnel_counts": reasons,
    }
    print(json.dumps(info, indent=2))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(info, fh, indent=2)
        print("report: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
