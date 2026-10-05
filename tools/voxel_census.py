# -*- coding: utf-8 -*-
"""
voxel_census.py -- what the campus is actually made of, from the voxel data.

The rendering rework needs two numbers before any geometry is written:

  * **which blocks** the campus is built from, so the material families (and
    therefore the CC0 material library) can be chosen from data rather than
    guessed; and
  * **how many faces** a face-culled reconstruction produces, because that is
    the triangle budget the whole approach has to fit in. Emitting all six faces
    of every block would be roughly six times that number and is not viable.

Both come from ``overworld.bin`` -- the layer-1 output that the earlier build
ignored in favour of primitive placeholder meshes.

    python3 tools/voxel_census.py --bin <overworld.bin> [--out <report.txt>]
"""

import argparse
import collections
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "phase2"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from voxelio import VoxelFile                     # noqa: E402
from block_families import family, FAMILIES       # noqa: E402

#: regions.campus from the layer-2 survey (block coordinates).
CAMPUS = (-144, 303, -544, 223)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, dest="bin_path")
    ap.add_argument("--out", default="")
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    x0, x1, z0, z1 = CAMPUS
    cx0, cx1 = x0 // 16, x1 // 16
    cz0, cz1 = z0 // 16, z1 // 16

    lines = []

    def say(m):
        lines.append(str(m))
        print(m)

    with VoxelFile(args.bin_path) as vf:
        hdr = vf.header
        say("=" * 70)
        say("voxel census  %s" % os.path.basename(args.bin_path))
        say("=" * 70)
        say("dimension=%s  chunks=%d  voxels=%d  palette=%d"
            % (hdr["dimension"], hdr["chunk_count"], hdr["voxel_count"],
               hdr["palette_count"]))
        say("campus blocks x[%d..%d] z[%d..%d] -> chunks x[%d..%d] z[%d..%d]"
            % (x0, x1, z0, z1, cx0, cx1, cz0, cz1))

        # ---- pass 1: block counts inside the campus ----------------------
        name_count = collections.Counter()
        fam_count = collections.Counter()
        total = 0
        ymin = 1 << 30
        ymax = -(1 << 30)
        chunks_seen = 0

        for ch in vf.iter_chunks():
            if not (cx0 <= ch["chunkX"] <= cx1 and cz0 <= ch["chunkZ"] <= cz1):
                continue
            xyz = ch["xyz"]
            st = ch["state"]
            m = ((xyz[:, 0] >= x0) & (xyz[:, 0] <= x1) &
                 (xyz[:, 2] >= z0) & (xyz[:, 2] <= z1))
            if not m.any():
                continue
            xyz = xyz[m]
            st = st[m]
            chunks_seen += 1
            total += int(st.size)
            ymin = min(ymin, int(xyz[:, 1].min()))
            ymax = max(ymax, int(xyz[:, 1].max()))

            for pi, c in zip(*np.unique(st, return_counts=True)):
                nm = vf.palette[int(pi)][0]
                name_count[nm] += int(c)
                fam_count[family(nm)] += int(c)

        say("")
        say("non-air blocks in campus: %d   (chunks contributing: %d)"
            % (total, chunks_seen))
        say("y range: %d .. %d" % (ymin, ymax))

        say("")
        say("--- material families ---")
        for fam, c in fam_count.most_common():
            say("  %-12s %9d  %5.1f%%" % (fam, c, 100.0 * c / max(1, total)))

        say("")
        say("--- top %d blocks ---" % args.top)
        for nm, c in name_count.most_common(args.top):
            say("  %-44s %9d  %5.2f%%  [%s]"
                % (nm, c, 100.0 * c / max(1, total), family(nm)))

        # ---- pass 2: exposed-face count (the triangle budget) ------------
        # A dense occupancy grid over the campus plus a one-block margin, so an
        # exposed face is simply a neighbour that is not occupied. This is the
        # number that decides whether a face-culled reconstruction is viable.
        W = (x1 - x0 + 1) + 2
        D = (z1 - z0 + 1) + 2
        H = (ymax - ymin + 1) + 2
        cells = W * D * H
        say("")
        say("occupancy grid: %d x %d x %d = %.1f M cells (%.1f MB as uint8)"
            % (W, H, D, cells / 1e6, cells / 1e6))

        if cells > 400e6:
            say("grid too large to materialise; skipping the face count")
        else:
            occ = np.zeros((W, H, D), dtype=np.uint8)
            for ch in vf.iter_chunks():
                if not (cx0 <= ch["chunkX"] <= cx1 and
                        cz0 <= ch["chunkZ"] <= cz1):
                    continue
                xyz = ch["xyz"]
                m = ((xyz[:, 0] >= x0) & (xyz[:, 0] <= x1) &
                     (xyz[:, 2] >= z0) & (xyz[:, 2] <= z1))
                if not m.any():
                    continue
                xyz = xyz[m]
                occ[xyz[:, 0] - x0 + 1,
                    xyz[:, 1] - ymin + 1,
                    xyz[:, 2] - z0 + 1] = 1

            solid = int(occ.sum())
            # faces = pairs where exactly one of the two neighbours is solid
            faces = 0
            for axis in (0, 1, 2):
                a = occ.take(range(0, occ.shape[axis] - 1), axis=axis)
                b = occ.take(range(1, occ.shape[axis]), axis=axis)
                faces += int(np.count_nonzero(a != b))

            say("")
            say("--- face-culled reconstruction ---")
            say("  solid blocks : %d" % solid)
            say("  exposed faces: %d  -> %d triangles"
                % (faces, faces * 2))
            say("  (all-6-faces would be %d faces -> %d triangles)"
                % (solid * 6, solid * 12))

        say("")
        say("--- done ---")

    if args.out:
        with open(args.out, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        print("report: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
