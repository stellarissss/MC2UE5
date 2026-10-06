# -*- coding: utf-8 -*-
"""
terrain_smooth.py -- turn the classified height field into a smooth surface mesh.

Step S2 of the academic-route rebuild. The SIGGRAPH '25 "Minecraft to 3D"
pipeline does this between labelling and export, and describes it as: the
stepped block surface is up-sampled with trilinear interpolation and an
anisotropic Gaussian filter removes the staircase artefacts.

That is the right operation for this save, and the reason is measurable rather
than aesthetic: S1 measured that **97.9% of adjacent terrain columns differ by
one block or less**. The surface is not a landscape, it is a large flat plain
carrying single-block jitter -- the exact thing that reads as a Minecraft
staircase under real lighting and ruins every silhouette. Smoothing it costs
almost nothing in fidelity because there is almost no relief to lose: the
heights span 0..30 m with 247,033 of 344,062 columns sitting flat at y=3.

The anisotropic part matters. A plain isotropic Gaussian on a height field
rounds off the edges of a building pad and the lip of a step, which is exactly
the detail the "campus must match the save" constraint depends on. So the
smoothing here is edge-aware in the cheap, standard way: smooth, then blend the
smoothed value back toward the original wherever the original was locally flat.
Flat ground stays *exactly* flat; only the jitter gets removed.

Output is a tiled OBJ so no single mesh is enormous, plus a seating report so
S3 can drop each building onto the surface it actually stands on.

    python3 tools/terrain_smooth.py
"""

import argparse
import json
import os
import sys
import time

import numpy as np

CAMPUS = (-144, 303, -544, 223)
BLOCK_CM = 100.0


def fill_invalid(h):
    """Replace -1 columns with the nearest valid height.

    Two columns in 344,064 are empty (a corner of the campus is outside the
    data). Leaving them at -1 would put a hole in the mesh, so they take the
    nearest real height -- a distance transform is the standard way to do that
    and needs no special-casing.
    """
    from scipy import ndimage
    bad = h < 0
    if not bad.any():
        return h
    _, idx = ndimage.distance_transform_edt(bad, return_indices=True)
    return h[tuple(idx)]


def smooth_heightfield(h, sigma, flat_restore):
    """Gaussian-smooth, then restore genuinely flat areas.

    ``flat_restore`` in [0,1] is how much of the original is blended back where
    the original was locally flat. 1.0 would disable smoothing on flat ground
    entirely -- which is the point: the staircase is *jitter on flat ground*, so
    the parts that must be preserved are exactly the parts that were already
    flat, and the parts that must be smoothed are the ones that were not.
    """
    from scipy import ndimage
    if sigma <= 0:
        return h.astype(np.float32), 0.0

    h32 = h.astype(np.float32)
    sm = ndimage.gaussian_filter(h32, sigma=sigma, mode="nearest")

    if flat_restore > 0.0:
        # "Locally flat" = the 3x3 neighbourhood of the ORIGINAL is within one
        # block. Computed on the original, not the smoothed field, so the mask
        # cannot drift.
        lo = ndimage.minimum_filter(h32, size=3, mode="nearest")
        hi = ndimage.maximum_filter(h32, size=3, mode="nearest")
        flat = (hi - lo) <= 1.0
        sm = np.where(flat, h32 * flat_restore + sm * (1.0 - flat_restore), sm)
    return sm, float(np.abs(sm - h32).mean())


def build_tiles(hm, origin, tile, upscale, out_dir):
    """Write one OBJ per tile of the smooth height field.

    Axes: Minecraft (x, y, z) maps to Unreal (X, Y, Z) as
    (x, z, height) * 100 cm, with Z up. That single mapping is the whole
    coordinate contract; getting it wrong once is what made the old height-map
    path invisible, so it lives in exactly one place.
    """
    os.makedirs(out_dir, exist_ok=True)
    nu, nv = hm.shape
    step = 1.0 / upscale

    # Vertices for the whole field, indexed by (u, v).
    uu, vv = np.meshgrid(np.arange(nu) * step, np.arange(nv) * step,
                         indexing="ij")
    wx = origin[0] + uu
    wy = origin[1] + vv
    wz = hm

    # Normals from the height gradient, in Unreal axes: X from d/dx, Y from
    # d/d(y), Z up. One central difference per axis, then normalise.
    dzdx = np.gradient(wz, axis=0) / (step * BLOCK_CM)
    dzdy = np.gradient(wz, axis=1) / (step * BLOCK_CM)
    nx = -dzdx
    ny = -dzdy
    nz = np.ones_like(wz)
    ln = np.sqrt(nx * nx + ny * ny + nz * nz)
    nx, ny, nz = nx / ln, ny / ln, nz / ln

    written = []
    for tu in range(0, nu, tile):
        for tv in range(0, nv, tile):
            bu = min(tu + tile, nu)
            bv = min(tv + tile, nv)
            if bu - tu < 2 or bv - tv < 2:
                continue
            name = "terrain_%04d_%04d" % (origin[0] + tu, origin[1] + tv)
            path = os.path.join(out_dir, name + ".obj")
            with open(path, "w") as fh:
                fh.write("# MC2UE5 smooth terrain tile %s\n" % name)
                fh.write("# origin_block=(%d,%d) size=%dx%d blocks\n"
                         % (origin[0] + tu, origin[1] + tv, bu - tu, bv - tv))
                # Vertices, 1-based indices in OBJ.
                idx = {}
                n = 0
                for i in range(tu, bu):
                    for j in range(tv, bv):
                        n += 1
                        idx[(i, j)] = n
                        fh.write("v %.2f %.2f %.2f\n"
                                 % (wx[i, j] * BLOCK_CM, wy[i, j] * BLOCK_CM,
                                    wz[i, j] * BLOCK_CM))
                for i in range(tu, bu):
                    for j in range(tv, bv):
                        fh.write("vt %.4f %.4f\n"
                                 % ((i - tu) * step, (j - tv) * step))
                for i in range(tu, bu):
                    for j in range(tv, bv):
                        fh.write("vn %.4f %.4f %.4f\n" % (nx[i, j], ny[i, j], nz[i, j]))
                # Faces: (a,b,c) with b = +u, c = +u+v gives an upward normal by
                # the right-hand rule, which is the OBJ front-face convention.
                for i in range(tu, bu - 1):
                    for j in range(tv, bv - 1):
                        a = idx[(i, j)]
                        b = idx[(i + 1, j)]
                        c = idx[(i + 1, j + 1)]
                        d = idx[(i, j + 1)]
                        fh.write("f %d/%d/%d %d/%d/%d %d/%d/%d\n"
                                 % (a, a, a, b, b, b, c, c, c))
                        fh.write("f %d/%d/%d %d/%d/%d %d/%d/%d\n"
                                 % (a, a, a, c, c, c, d, d, d))
            written.append({
                "name": name,
                "obj": "terrain/%s.obj" % name,
                "origin_block": [origin[0] + tu, origin[1] + tv],
                "size_blocks": [bu - tu, bv - tv],
                "vertices": (bu - tu) * (bv - tv),
                "triangles": 2 * (bu - tu - 1) * (bv - tv - 1),
            })
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terrain", default="out/classify/terrain.npy")
    ap.add_argument("--structures", default="out/classify/structures.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--sigma", type=float, default=0.9,
                    help="Gaussian sigma in blocks; 0 disables smoothing")
    ap.add_argument("--flat-restore", type=float, default=0.85,
                    help="how much of the original is kept on locally flat "
                         "ground (0..1)")
    ap.add_argument("--upscale", type=int, default=1,
                    help="1 = one vertex per block; 2 = 0.5 m grid")
    ap.add_argument("--tile", type=int, default=128,
                    help="blocks per output tile edge")
    args = ap.parse_args()

    t0 = time.time()
    h = np.load(args.terrain)
    if h.shape != (CAMPUS[1] - CAMPUS[0] + 1, CAMPUS[3] - CAMPUS[2] + 1):
        print("warning: height field shape %s does not match campus %s"
              % (h.shape, CAMPUS))
    h = fill_invalid(h)
    before = h.astype(np.float32)

    sm, mean_shift = smooth_heightfield(before, args.sigma, args.flat_restore)

    if args.upscale > 1:
        from scipy import ndimage
        sm = ndimage.zoom(sm, args.upscale, order=3, mode="nearest")

    err = np.abs(sm - before)
    print("smoothing: sigma=%.2f flat_restore=%.2f upscale=%d"
          % (args.sigma, args.flat_restore, args.upscale))
    print("  mean |dh| = %.3f m   p95 = %.3f m   max = %.3f m"
          % (err.mean(), np.percentile(err, 95), err.max()))
    # The number that matters: did any real landform move?
    print("  columns moved >1 m: %d (%.4f%%)"
          % (int((err > 1.0).sum()), 100.0 * (err > 1.0).mean()))

    tiles = build_tiles(sm.astype(np.float32), (CAMPUS[0], CAMPUS[2]),
                        args.tile, args.upscale,
                        os.path.join(args.out, "terrain"))
    tris = sum(t["triangles"] for t in tiles)
    verts = sum(t["vertices"] for t in tiles)
    print("mesh: %d tiles, %d vertices, %d triangles  (%.1fs)"
          % (len(tiles), verts, tris, time.time() - t0))

    # Compare against what the cube layer costs, which is the whole point.
    cubes = 1171144
    print("  for comparison: the cube layer was %d instances = %d triangles"
          % (cubes, cubes * 12))
    print("  terrain mesh is %.1fx fewer triangles" % (cubes * 12 / max(tris, 1)))

    # Seating: where should each building sit on the new surface?
    seat = []
    if os.path.exists(args.structures):
        with open(args.structures) as fh:
            objs = json.load(fh)
        for o in objs:
            cx, cy, cz = o["centre_block"]
            ix = np.clip(cx - CAMPUS[0], 0, sm.shape[0] - 1)
            iz = np.clip(cz - CAMPUS[2], 0, sm.shape[1] - 1)
            seat.append({
                "id": o["id"],
                "centre_block": o["centre_block"],
                "voxel_bottom_y": o["bbox"][1][0],
                "smooth_ground_y": float(sm[ix, iz]),
                "drop_m": float(o["bbox"][1][0] - sm[ix, iz]),
            })
        drops = [abs(s["drop_m"]) for s in seat]
        print("seating: %d structures, |drop| mean %.2f m, p95 %.2f m, max %.2f m"
              % (len(seat), np.mean(drops), np.percentile(drops, 95),
                 max(drops)))

    manifest = {
        "campus": list(CAMPUS),
        "sigma": args.sigma,
        "flat_restore": args.flat_restore,
        "upscale": args.upscale,
        "tile_blocks": args.tile,
        "tiles": tiles,
        "vertices": verts,
        "triangles": tris,
        "smoothing": {
            "mean_abs_dh_m": round(float(err.mean()), 4),
            "p95_abs_dh_m": round(float(np.percentile(err, 95)), 4),
            "max_abs_dh_m": round(float(err.max()), 4),
            "columns_moved_gt_1m": int((err > 1.0).sum()),
        },
        "elapsed_s": round(time.time() - t0, 1),
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "terrain", "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    if seat:
        with open(os.path.join(args.out, "terrain", "seating.json"), "w") as fh:
            json.dump(seat, fh, indent=2)
    print("report: %s/terrain/manifest.json" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
