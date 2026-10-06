# -*- coding: utf-8 -*-
"""
campus_map.py -- top-down ASCII map of the campus, by material role.

Placing a camera needs to know what is where. This prints the campus as a grid
of columns, each labelled by what dominates the surface there, which is also how
to check at a glance that the UE scene corresponds to the save (the core
constraint): the field has to appear where the field is, the buildings where the
buildings are.

Legend: F field/wool  B building stone  G grass  T tree/leaves  W water
        # other/structures  .  empty column
"""

import os
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "phase2"))
from voxelio import VoxelFile                     # noqa: E402

CAMPUS = (-144, 303, -544, 223)
STEP = 12          # blocks per map cell


def role(name):
    n = name.split(":", 1)[-1]
    if "wool" in n:
        return "F"
    if n in ("white_concrete", "light_gray_concrete", "quartz_block",
             "smooth_quartz", "smooth_sandstone"):
        return "B"
    if "leaves" in n or "log" in n:
        return "T"
    if n in ("water",):
        return "W"
    if n in ("grass_block", "grass", "dirt_path", "grass_path"):
        return "G"
    return "#"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "voxel_data/full/overworld.bin"
    x0, x1, z0, z1 = CAMPUS
    nx = (x1 - x0) // STEP + 1
    nz = (z1 - z0) // STEP + 1
    cells = [[Counter() for _ in range(nz)] for _ in range(nx)]
    topy = [[-1] * nz for _ in range(nx)]

    with VoxelFile(path) as vf:
        for ch in vf.iter_chunks():
            cx, cz = ch["chunkX"], ch["chunkZ"]
            if (cx * 16 > x1 or cx * 16 + 15 < x0
                    or cz * 16 > z1 or cz * 16 + 15 < z0):
                continue
            if ch["state"].size == 0:
                continue
            xyz, st = ch["xyz"], ch["state"]
            for s in np.unique(st):
                nm = vf.palette[int(s)][0]
                m = st == s
                ix = (xyz[m, 0] - x0) // STEP
                iz = (xyz[m, 2] - z0) // STEP
                iy = xyz[m, 1]
                r = role(nm)
                for a, b, y in zip(ix, iz, iy):
                    if 0 <= a < nx and 0 <= b < nz:
                        cells[a][b][r] += 1
                        if y > topy[a][b]:
                            topy[a][b] = int(y)

    print("campus x[%d..%d] z[%d..%d]  step=%d blocks  (top of map = z min)"
          % (x0, x1, z0, z1, STEP))
    print("     " + "".join(str((z0 + j * STEP) // 100 % 10) for j in range(nz)))
    for i in range(nx):
        row = []
        for j in range(nz):
            c = cells[i][j]
            if not c:
                row.append(".")
            else:
                # Ignore the ubiquitous ground cover when a real structure is
                # present, so buildings and fields are not hidden by the grass
                # they stand on.
                struct = {k: v for k, v in c.items() if k not in ("G", "#")}
                row.append(max(struct, key=struct.get) if struct
                           else max(c, key=c.get))
        print("%4d " % (x0 + i * STEP) + "".join(row))

    # Landmarks as map coordinates, to aim a camera at.
    print()
    print("landmark anchors (block coords):")
    print("  field  centroid ~ (96, -212)")
    print("  build  centroid ~ (8, -354)")
    print("  tall   buildings: (-36,-300) top40  (-28,-500) top61  (-101,-300) top48")


if __name__ == "__main__":
    main()
