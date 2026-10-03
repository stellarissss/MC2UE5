#!/usr/bin/env python3
"""
Voxel-only pass over voxel_data/full/*.bin that reports, per dimension:

  * world bounds, cell grid size, and the number of spatial cells in use
  * the exact number of (cell x blockName) HISM groups the importer would build
  * the exact total instance count
  * any blockName that has no material in assets/material_manifest.json

It reads the MC2WV2 layout documented in parse/INTERMEDIATE_FORMAT.md and shares
no code with parse_world.py, so it also doubles as a second opinion on the files.

Usage:
    python3 scripts/hism_estimate.py [--cell 512] [--json out.json]
"""
import argparse
import collections
import json
import os
import struct
import sys

import numpy as np

FILE_MAGIC = b"MC2WV2\0\0"
HEADER = struct.Struct("<8sIB3sQQQQQQ")
CHUNK_TAB = struct.Struct("<iiHHIQ")
DIM_NAMES = {0: "overworld", 1: "nether", 2: "end"}

# (cellX, cellZ) -> single int64 key. MC coords are small (|x| < 3000 blocks),
# so 2**21 cells of headroom on each axis is far more than enough.
CELL_SPAN = 1 << 22
CELL_BIAS = 1 << 21


def read_header(f):
    (magic, version, dim_id, _rsv, n_chunks, n_voxels, n_pal,
     pal_off, tab_off, vox_off) = HEADER.unpack(f.read(HEADER.size))
    if magic != FILE_MAGIC:
        raise IOError("bad magic %r (expected MC2WV2)" % (magic,))
    if version != 2:
        raise IOError("unsupported version %d (this tool understands v2)" % version)
    return {"dimension": DIM_NAMES.get(dim_id, str(dim_id)), "chunks": n_chunks,
            "voxels": n_voxels, "palette": n_pal, "pal_off": pal_off,
            "tab_off": tab_off, "vox_off": vox_off}


def read_palette(f, off):
    f.seek(off)
    (n,) = struct.unpack("<I", f.read(4))
    names = []
    for _ in range(n):
        nlen, plen = struct.unpack("<HH", f.read(4))
        names.append(f.read(nlen).decode("utf-8"))
        f.read(plen)
    return names


def read_chunk_table(f, off, count):
    f.seek(off)
    rows = []
    for _ in range(count):
        cx, cz, pcount, _pad, vcount, voff = CHUNK_TAB.unpack(f.read(CHUNK_TAB.size))
        gmap = np.frombuffer(f.read(4 * pcount), dtype=np.uint32) if pcount else np.zeros(0, np.uint32)
        rows.append((cx, cz, vcount, voff, gmap))
    return rows


def analyse(path, cell, manifest_names, max_per_hism=0):
    f = open(path, "rb")
    h = read_header(f)
    names = read_palette(f, h["pal_off"])
    rows = read_chunk_table(f, h["tab_off"], h["chunks"])

    groups = collections.Counter()   # (cellX, cellZ, blockName) -> instances
    per_type = collections.Counter()
    unmapped = collections.Counter()
    bounds = [10 ** 9, -10 ** 9, 10 ** 9, -10 ** 9, 10 ** 9, -10 ** 9]  # x,z,y min/max

    for cx, cz, vcount, voff, gmap in rows:
        f.seek(voff)
        w = np.frombuffer(f.read(vcount * 4), dtype=np.uint32)
        dx = (w & 0xF).astype(np.int64)
        dz = ((w >> 4) & 0xF).astype(np.int64)
        wy = ((w >> 8) & 0x1FF).astype(np.int64)
        li = ((w >> 17) & 0x7FFF).astype(np.int64)

        gidx = gmap[li] if gmap.size else li
        wx = cx * 16 + dx
        wz = cz * 16 + dz

        bounds[0] = min(bounds[0], int(wx.min())); bounds[1] = max(bounds[1], int(wx.max()))
        bounds[2] = min(bounds[2], int(wz.min())); bounds[3] = max(bounds[3], int(wz.max()))
        bounds[4] = min(bounds[4], int(wy.min())); bounds[5] = max(bounds[5], int(wy.max()))

        # Encode (cellX, cellZ) into one int64 so np.unique can group in one
        # pass. A plain `cx*K + cz` breaks for negative cellZ (Python floor
        # division), so bias cellZ into a non-negative range first.
        cellx = wx // cell
        cellz = wz // cell
        cellkey = cellx * CELL_SPAN + (cellz + CELL_BIAS)
        uniq, inv = np.unique(gidx, return_inverse=True)
        for rank, gu in enumerate(uniq):
            name = names[int(gu)]
            sel = inv == rank
            cnt = int(sel.sum())
            per_type[name] += cnt
            if name not in manifest_names:
                unmapped[name] += cnt
                continue
            cc, ccnt = np.unique(cellkey[sel], return_counts=True)
            for a, b in zip(cc, ccnt):
                groups[(int(a) // CELL_SPAN, int(a) % CELL_SPAN - CELL_BIAS, name)] += int(b)

    cells = set((a, b) for (a, b, _n) in groups)
    # A single HISM holding millions of instances is pathological to build and
    # to save, so the importer splits oversized groups into several components.
    split = 0
    if max_per_hism > 0:
        for _k, v in groups.items():
            split += (v + max_per_hism - 1) // max_per_hism
    return {
        "dimension": h["dimension"],
        "file": os.path.relpath(path),
        "bytes": os.path.getsize(path),
        "chunks": h["chunks"],
        "voxels": h["voxels"],
        "x_range": [bounds[0], bounds[1]],
        "y_range": [bounds[4], bounds[5]],
        "z_range": [bounds[2], bounds[3]],
        "cells_in_use": len(cells),
        "cell_extent": [max(c[0] for c in cells) - min(c[0] for c in cells) + 1,
                        max(c[1] for c in cells) - min(c[1] for c in cells) + 1],
        "hism_groups": len(groups),
        "hism_instances": int(sum(groups.values())),
        "hism_components_with_split": split or None,
        "block_types": len(per_type),
        "unmapped_block_types": len(unmapped),
        "unmapped_instances": int(sum(unmapped.values())),
        "unmapped_top": [[n, c] for n, c in unmapped.most_common(5)],
        "largest_groups": [{"cell": [a, b], "block": n, "instances": c}
                           for (a, b, n), c in groups.most_common(5)],
    }, groups


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, os.pardir))
    ap = argparse.ArgumentParser()
    ap.add_argument("--voxel-dir", default=os.path.join(root, "voxel_data", "full"))
    ap.add_argument("--manifest", default=os.path.join(root, "assets", "material_manifest.json"))
    ap.add_argument("--cell", type=int, default=512)
    ap.add_argument("--max-per-hism", type=int, default=500000,
                    help="split any (cell x block) group larger than this into "
                         "several HISM components (0 = do not split)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    manifest_names = {e["blockName"] for e in json.load(open(args.manifest))}

    out = {"cell_size": args.cell, "max_instances_per_hism": args.max_per_hism,
           "dimensions": [], "totals": {}}
    all_groups = collections.Counter()
    total_bytes = total_inst = total_groups = total_components = 0
    for dim in ("overworld", "nether", "end"):
        path = os.path.join(args.voxel_dir, "%s.bin" % dim)
        if not os.path.isfile(path):
            print("missing %s -- run parse_world.py --full first" % path, file=sys.stderr)
            return 1
        info, groups = analyse(path, args.cell, manifest_names, args.max_per_hism)
        out["dimensions"].append(info)
        all_groups.update(groups)
        total_bytes += info["bytes"]
        total_inst += info["hism_instances"]
        total_groups += info["hism_groups"]
        total_components += info["hism_components_with_split"] or info["hism_groups"]
        print("[%s] %s" % (info["dimension"], info["file"]))
        print("   chunks=%d voxels=%d (%.1f MiB)" % (info["chunks"], info["voxels"],
                                                      info["bytes"] / 1048576.0))
        print("   x%s y%s z%s" % (info["x_range"], info["y_range"], info["z_range"]))
        print("   cells_in_use=%d (extent %dx%d)  groups=%d  instances=%d  -> HISM components=%d"
              % (info["cells_in_use"], info["cell_extent"][0], info["cell_extent"][1],
                 info["hism_groups"], info["hism_instances"],
                 info["hism_components_with_split"] or info["hism_groups"]))
        if info["unmapped_block_types"]:
            print("   !! %d block types / %d instances have NO material: %s"
                  % (info["unmapped_block_types"], info["unmapped_instances"],
                     info["unmapped_top"][:3]))

    out["totals"] = {"bytes": total_bytes, "mib": round(total_bytes / 1048576.0, 1),
                     "hism_groups": total_groups, "hism_instances": total_inst,
                     "hism_components": total_components}
    print("\nTOTAL %.1f MiB | groups=%d | HISM components=%d | instances=%d"
          % (total_bytes / 1048576.0, total_groups, total_components, total_inst))
    print("largest groups:")
    for (a, b, n), c in all_groups.most_common(6):
        print("   cell(%d,%d) %-40s %d" % (a, b, n, c))

    if args.json:
        out["largest_groups"] = [{"cell": [a, b], "block": n, "instances": c}
                                for (a, b, n), c in all_groups.most_common(40)]
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print("wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
