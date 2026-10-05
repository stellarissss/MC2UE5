# -*- coding: utf-8 -*-
"""
export_heightmaps.py -- emit the raw 16-bit heightfields the game loads.

The terrain is built at runtime (AMCFrameCaptureGameMode::BuildProceduralTerrain)
from one little-endian uint16 heightfield per tile, shipped as
``project/Terrain/<tile>.u16``. They are generated here rather than derived in
engine because the OBJ importer is not usable for this data: it reads only the
first ~640 vertices of a file, so a 196,944-vertex terrain tile imports as 2,179
vertices with no usable geometry or collision.

Source of truth is the committed phase-2 heightmap PNGs, so these files are
reproducible and do not need to be treated as data:

    python3 tools/export_heightmaps.py --root <checkout> --out <project>/Terrain
"""

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_terrain_mesh as btm            # noqa: E402  (same directory)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="MC2UE5 checkout root")
    ap.add_argument("--out", required=True, help="directory to write .u16 files")
    ap.add_argument("--dim", default="overworld")
    args = ap.parse_args()

    lsc = os.path.join(args.root, "out", "phase2", args.dim, "landscape")
    meta = json.load(open(os.path.join(lsc, "landscape.json")))
    os.makedirs(args.out, exist_ok=True)

    total = 0
    for tile in meta["tiles"]:
        png = os.path.join(lsc, tile["file"])
        w, h, rows, ch = btm.read_heightmap(png)
        if w != tile["resolution"][0] or h != tile["resolution"][1]:
            print("  %s: %dx%d != metadata %dx%d -- skipping"
                  % (tile["file"], w, h, tile["resolution"][0],
                     tile["resolution"][1]))
            continue

        # Keep every byte of the height column, including any that are
        # unambiguous: discarding rows would silently shift the terrain.
        name = os.path.splitext(tile["file"])[0]
        out = os.path.join(args.out, name + ".u16")
        with open(out, "wb") as fh:
            for r in range(h):
                line = rows[r]
                vals = []
                for c in range(w):
                    i = (c * ch) * 2
                    (v,) = struct.unpack(">H", bytes(line[i:i + 2]))
                    vals.append(v)
                fh.write(struct.pack("<%dH" % w, *vals))
        size = os.path.getsize(out)
        total += size
        print("  %-20s %3dx%-4d %8d bytes" % (name, w, h, size))

    print("=" * 50)
    print("%d tiles, %.2f MB" % (len(meta["tiles"]), total / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
