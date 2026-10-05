# -*- coding: utf-8 -*-
"""
pick_vantage.py -- choose a player start that actually shows the campus.

The campus now contains 1.17M real blocks, so "where does the player appear" is
no longer a detail: the previous spawn sat inside a block and the first frame
was the inside of a wall.

This reads the voxel data and picks a start from measurements rather than
guesswork:

  * **ground height** per column, so the player stands on the surface;
  * **openness**, so the spot is not inside solid geometry or under a roof;
  * **sight line** toward the building mass, used to derive the yaw.

    python3 tools/pick_vantage.py --bin <overworld.bin> [--out <json>]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, dest="bin_path")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    x0, x1, z0, z1 = CAMPUS
    W, D = x1 - x0 + 1, z1 - z0 + 1
    H = 64
    # Occupancy plus the highest solid block per column.
    occ = np.zeros((W, H, D), dtype=bool)
    top = np.full((W, D), -1, dtype=np.int16)
    solid = np.zeros((W, D), dtype=np.int32)
    # Mass centre of the building materials, used for the sight line.
    bx = bz = bn = 0
    bx_s = bz_s = 0.0

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
            ix, iz, iy = ix[keep], iz[keep], iy[keep]
            st = st[keep]
            occ[ix, iy, iz] = True
            np.maximum.at(top, (ix, iz), iy)
            np.add.at(solid, (ix, iz), 1)
            for s in np.unique(st):
                nm = vf.palette[int(s)][0].split(":", 1)[-1]
                if nm in ("white_concrete", "light_gray_concrete", "quartz_block"):
                    m = st == s
                    bx_s += float(xyz[keep][:, 0][m].sum())
                    bz_s += float(xyz[keep][:, 2][m].sum())
                    bn += int(m.sum())

    build_x = bx_s / bn if bn else float(np.mean(np.arange(W))) + x0
    build_z = bz_s / bn if bn else float(np.mean(np.arange(D))) + z0

    # "Open" = little solid material in the column and sky above the surface.
    sky_clear = np.zeros((W, D), dtype=bool)
    for ix in range(W):
        for iz in range(D):
            t = top[ix, iz]
            if t < 0:
                continue
            sky_clear[ix, iz] = not occ[ix, t + 1:, iz].any()

    dist = np.sqrt((np.arange(W)[:, None] + x0 - build_x) ** 2 +
                   (np.arange(D)[None, :] + z0 - build_z) ** 2)

    # Want: standing on ground (low surface height), an open column, in the
    # 90-190 block band from the buildings (close enough to read them, far
    # enough to see more than one).
    cand = (solid < 40) & sky_clear & (top >= 3) & (top <= 12) \
        & (dist > 90) & (dist < 190)
    if not cand.any():
        cand = sky_clear & (dist > 60) & (dist < 260)

    score = -np.abs(dist - 140.0) - solid * 0.2 + top * 0.0
    score = np.where(cand, score, -1e9)
    ix, iz = np.unravel_index(int(np.argmax(score)), score.shape)
    px, pz = x0 + int(ix), z0 + int(iz)
    py = int(top[ix, iz])

    # Yaw from the vantage toward the building mass (Unreal: X fwd, Y right).
    dx = build_x - px
    dz = build_z - pz
    yaw = float(np.degrees(np.arctan2(dz, dx)))

    info = {
        "spawn_block": [px, pz],
        "surface_block": py,
        "spawn_cm": [px * BLOCK_CM, pz * BLOCK_CM,
                     py * BLOCK_CM + 3 * BLOCK_CM],
        "yaw": round(yaw, 1),
        "building_core_block": [round(build_x), round(build_z)],
        "distance_to_core_blocks": round(float(dist[ix, iz]), 1),
        "column_solid_blocks": int(solid[ix, iz]),
        "sky_clear": bool(sky_clear[ix, iz]),
        "candidates": int(cand.sum()),
    }
    print(json.dumps(info, indent=2))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(info, fh, indent=2)
        print("report: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
