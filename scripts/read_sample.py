#!/usr/bin/env python3
"""
Standalone reader for the MC2UE5 intermediate voxel format (.bin).

Decodes strictly per parse/INTERMEDIATE_FORMAT.md and shares no code with
parse_world.py, so it doubles as a conformance check on the writer.

Usage:
    python3 read_sample.py [file.bin] [--limit N] [--chunk CX,CZ]

Prints a summary and, with --limit, the first N decoded voxels as
(worldX, worldY, worldZ, name, properties).
"""
import argparse
import collections
import os
import struct
import sys

FILE_MAGIC = b"MC2WV2\0\0"
HEADER = struct.Struct("<8sIB3sQQQQQQ")
CHUNK_TAB = struct.Struct("<iiHHIQ")
DIM_NAMES = {0: "overworld", 1: "nether", 2: "end"}


def unpack_voxel(word):
    """MC2WV2 voxel word -> (dx, worldY, dz, chunkLocalPaletteIndex)."""
    return (word & 0xF,
            (word >> 8) & 0x1FF,
            (word >> 4) & 0xF,
            (word >> 17) & 0x7FFF)


def read_header(f):
    raw = f.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise IOError("file too short for header")
    (magic, version, dim_id, _reserved, n_chunks, n_voxels, n_pal,
     pal_off, tab_off, vox_off) = HEADER.unpack(raw)
    if magic != FILE_MAGIC:
        raise IOError("bad magic %r (expected %r)" % (magic, FILE_MAGIC))
    return {
        "version": version, "dimension": DIM_NAMES.get(dim_id, str(dim_id)),
        "chunk_count": n_chunks, "voxel_count": n_voxels,
        "palette_count": n_pal, "palette_offset": pal_off,
        "chunk_table_offset": tab_off, "voxel_offset": vox_off,
    }


def read_palette(f, off, count):
    f.seek(off)
    (n,) = struct.unpack("<I", f.read(4))
    if n != count:
        raise IOError("palette count mismatch: header %d, payload %d" % (count, n))
    out = []
    for _ in range(n):
        nlen, plen = struct.unpack("<HH", f.read(4))
        name = f.read(nlen).decode("utf-8")
        props = f.read(plen).decode("utf-8")
        out.append((name, props))
    return out


def read_chunk_table(f, off, count):
    f.seek(off)
    rows = []
    for _ in range(count):
        cx, cz, pcount, _pad, vcount, voff = CHUNK_TAB.unpack(f.read(CHUNK_TAB.size))
        gmap = list(struct.unpack("<%dI" % pcount, f.read(4 * pcount))) if pcount else []
        rows.append({"cx": cx, "cz": cz, "vcount": vcount, "voff": voff, "gmap": gmap})
    return rows


def decode_chunk(f, row, palette):
    """Yield (worldX, worldY, worldZ, name, properties) for one chunk.

    v2 chunk-table coords are ABSOLUTE chunk coords, so no region hint needed.
    """
    f.seek(row["voff"])
    data = f.read(row["vcount"] * 4)
    if len(data) != row["vcount"] * 4:
        raise IOError("truncated voxel block for chunk (%d,%d)"
                      % (row["cx"], row["cz"]))
    gmap = row["gmap"]
    base_x = row["cx"] * 16
    base_z = row["cz"] * 16
    for (word,) in struct.iter_unpack("<I", data):
        dx, wy, dz, li = unpack_voxel(word)
        name, props = palette[gmap[li]]
        yield base_x + dx, wy, base_z + dz, name, props


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         os.pardir, "voxel_data", "sample_overworld.bin"))
    ap.add_argument("--limit", type=int, default=0, help="print first N voxels")
    ap.add_argument("--chunk", help="only this chunk, e.g. -3,7")
    ap.add_argument("--stats", action="store_true", help="block histogram only")
    args = ap.parse_args()

    path = os.path.abspath(args.file)

    with open(path, "rb") as f:
        h = read_header(f)
        size = os.path.getsize(path)
        print("file      : %s (%.2f KiB)" % (path, size / 1024.0))
        print("version   : %d   dimension: %s" % (h["version"], h["dimension"]))
        print("chunks    : %d" % h["chunk_count"])
        print("voxels    : %d" % h["voxel_count"])
        print("palette   : %d entries" % h["palette_count"])
        print("segments  : palette@%d chunktable@%d voxels@%d"
              % (h["palette_offset"], h["chunk_table_offset"], h["voxel_offset"]))

        palette = read_palette(f, h["palette_offset"], h["palette_count"])
        rows = read_chunk_table(f, h["chunk_table_offset"], h["chunk_count"])

        # structural check: voxel extents must tile the voxel section exactly
        cur = h["voxel_offset"]
        for r in rows:
            if r["voff"] != cur:
                raise IOError("voxel offset gap at chunk (%d,%d): %d != %d"
                              % (r["cx"], r["cz"], r["voff"], cur))
            cur += r["vcount"] * 4
        print("layout    : OK (voxel section ends at %d, file %d)" % (cur, size))

        want = None
        if args.chunk:
            want = tuple(int(x) for x in args.chunk.split(","))

        total = 0
        hist = collections.Counter()
        printed = 0
        for r in rows:
            if want and (r["cx"], r["cz"]) != want:
                continue
            for wx, wy, wz, name, props in decode_chunk(f, r, palette):
                total += 1
                hist[(name, props)] += 1
                if args.limit and printed < args.limit:
                    print("  (%5d,%3d,%5d)  %-34s %s" % (wx, wy, wz, name, props))
                    printed += 1

        print("decoded   : %d voxels across %d chunks"
              % (total, len(rows) if not want else 1))
        if args.stats or not args.limit:
            print("top 10 states:")
            for (name, props), n in hist.most_common(10):
                print("  %-34s %-46s %d" % (name, props, n))


if __name__ == "__main__":
    main()
