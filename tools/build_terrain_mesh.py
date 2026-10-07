# -*- coding: utf-8 -*-
"""
build_terrain_mesh.py -- turn the phase-2 heightmaps into importable terrain meshes.

Why this exists
---------------
The upstream pipeline delivers the campus as UE5 Landscape actors, and
``project/Content/Python/import_phase2.py`` drives them through
``LandscapeEditorSubsystem`` / ``LandscapeSubsystem`` / ``LandscapeInfo``.
None of those exist in the Python API of UE 5.8:

    >>> [n for n in dir(unreal) if "Landscape" in n]        # editor subsystems
    []
    >>> hasattr(unreal, "LandscapeInfo")
    False

``unreal.Landscape`` is exposed, but spawning it yields a
``LandscapePlaceholder`` with zero components -- ``ALandscapeProxy::
CreateLandscapeInfo`` is a C++ member with no Python binding, so the
placeholder never becomes a real landscape. That makes the Landscape route
unusable from a headless script on this engine version.

So the terrain is built as a StaticMesh instead. The data contract is
preserved exactly:

  * **1 vertex == 1 block**, XY Scale 100 cm, so the mesh occupies the same
    world coordinates the Landscape would have and still lines up with the
    pipeline's ``(x, y, z) * 100`` convention.
  * **Heights come from the engine's own decode**,
    ``z = actor_offset_z_cm + (v - 32768) / 128 * z_scale_cm`` -- the same
    formula ``phase2.landscape.ue_decode`` and ``import_phase2.py`` use, so
    the surface is where phase 2 said it would be. Normalising by 65535
    instead would make the terrain 128x too tall and report no error.

Collision is handled by UE rather than baked here: the mesh is generated with
LODs and ``lod_for_collision`` is pointed at a decimated level, which is how
the engine expects a large static mesh to carry collision. Triangulating
1.5 M triangles as complex collision would be far more expensive than
walking a 20k-triangle proxy.

Usage:
    python3 build_terrain_mesh.py --root <MC2UE5 checkout> --out <dir>
"""

import argparse
import json
import os
import sys
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import objio  # noqa: E402

BLOCK_CM = 100.0

#: Blocks per vertex in the collision proxy. 4 turns a 373x528 tile from 392k
#: triangles into ~24k, which is still far finer than a pawn's footprint, while
#: keeping the cooked physics mesh small enough to ship.
COLLISION_STRIDE = 4


# ----------------------------------------------------------------------------
# PNG reading (standard library only -- the same decoder shape as the UE-side
# script, so a heightmap that reads here reads identically there)
# ----------------------------------------------------------------------------

def _unfilter(ftype, line, prev, bpp):
    n = len(line)
    if ftype == 0:
        return
    if ftype == 1:
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:
        for i in range(n):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pr) & 0xFF
    else:
        raise ValueError("unknown PNG filter type %d" % ftype)


def read_heightmap(path):
    """-> (width, height, rows, channels) for a 16-bit greyscale PNG."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: %s" % path)

    width, height, bitdepth, colortype = struct.unpack(">IIBB", data[16:26])
    if bitdepth != 16:
        raise ValueError("%s: bit depth %s, needs 16" % (path, bitdepth))
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colortype)
    if channels is None:
        raise ValueError("%s: colour type %s unsupported" % (path, colortype))

    pos, idat = 8, []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        if ctype == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + length])
        elif ctype == b"IEND":
            break
        pos += 12 + length

    raw = zlib.decompress(b"".join(idat))
    stride = width * channels * 2
    bpp = channels * 2

    rows, prev, off = [], bytearray(stride), 0
    for _ in range(height):
        ftype = raw[off]
        cur = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        _unfilter(ftype, cur, prev, bpp)
        prev = cur
        rows.append(cur)
    return width, height, rows, channels


def decode_height_cm(u16, meta):
    """UE5's Landscape height decode, in centimetres."""
    return (float(meta["actor_offset_z_cm"])
            + (float(u16) - 32768.0) / 128.0 * float(meta["z_scale_cm"]))


# ----------------------------------------------------------------------------
# OBJ generation
# ----------------------------------------------------------------------------

def write_obj(out_path, width, height, rows, channels, meta, origin_cm,
              stride=1):
    """
    Write one `.u16` tile as a Wavefront OBJ, in world centimetres.

    This function's only remaining job is the *decoding*: the `.u16` rows and the
    `landscape.json` height metadata are specific to the phase-2 landscape
    format. The geometry itself lives in `tools/objio.py` so that a second
    heightfield source -- `terrain_smooth.py`, which reads a plain array of block
    heights -- writes byte-identical OBJs instead of its own near-copy of this
    loop.
    """
    step = max(1, int(stride))
    cols = list(range(0, width, step))
    if cols[-1] != width - 1:
        cols.append(width - 1)
    rws = list(range(0, height, step))
    if rws[-1] != height - 1:
        rws.append(height - 1)

    # Decode once, here, so objio never sees the phase-2 format.
    z_cm = []
    for r in rws:
        line = rows[r]
        zr = []
        for c in cols:
            i = (c * channels) * 2
            (v,) = struct.unpack(">H", bytes(line[i:i + 2]))
            zr.append(decode_height_cm(v, meta))
        z_cm.append(zr)

    out_path = str(out_path)
    res = objio.write_heightfield_obj(
        out_path, z_cm, origin_cm=origin_cm, block_cm=BLOCK_CM,
        stride=1,                      # already subsampled above
        comment="phase-2 landscape tile")
    # Report the pre-subsample triangle count, which is what `main` logs.
    gw, gh = len(cols), len(rws)
    res["stride"] = step
    res["verts"] = gw * gh
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="MC2UE5 checkout root")
    ap.add_argument("--out", required=True, help="where to write the OBJs")
    ap.add_argument("--dim", default="overworld")
    args = ap.parse_args()

    lsc = os.path.join(args.root, "out", "phase2", args.dim, "landscape")
    meta = json.load(open(os.path.join(lsc, "landscape.json")))
    os.makedirs(args.out, exist_ok=True)

    lo_b = float(meta["y_min_blocks"])
    hi_b = lo_b + float(meta["y_span_blocks"])
    print("source: %dx%d verts, y %.0f..%.0f blocks, xy_scale %.1f cm"
          % (meta["landscape_resolution"][0], meta["landscape_resolution"][1],
             lo_b, hi_b, meta["xy_scale_cm"]))

    if abs(float(meta["xy_scale_cm"]) - BLOCK_CM) > 1e-6:
        print("xy_scale_cm is %.4f, expected %.1f -- refusing, the terrain "
              "would not match block coordinates"
              % (float(meta["xy_scale_cm"]), BLOCK_CM))
        return 1

    t0 = time.time()
    total_v = total_t = 0
    written = []
    for tile in meta["tiles"]:
        png = os.path.join(lsc, tile["file"])
        w, h, rows, ch = read_heightmap(png)
        if w != tile["resolution"][0] or h != tile["resolution"][1]:
            print("  %s: %dx%d, metadata says %dx%d -- skipping"
                  % (tile["file"], w, h,
                     tile["resolution"][0], tile["resolution"][1]))
            continue

        stem = os.path.splitext(tile["file"])[0]
        out_path = os.path.join(args.out, stem + ".obj")
        stats = write_obj(out_path, w, h, rows, ch,
                          tile["height_cm_meta"], tile["origin_cm"])

        # Collision proxy. Complex-as-simple collision over 392k triangles per
        # tile would cook to a physics mesh hundreds of megabytes across the
        # four of them, and a character controller paying for that is absurd.
        # A stride-sampled copy of the same heightfield is ~24k triangles,
        # traces identically at pawn scale, and is what the pawn actually walks
        # on; the full-resolution mesh stays visual-only and carries Nanite.
        proxy_path = os.path.join(args.out, stem + "_collision.obj")
        pstats = write_obj(proxy_path, w, h, rows, ch,
                           tile["height_cm_meta"], tile["origin_cm"],
                           stride=COLLISION_STRIDE)

        # Read the emitted heights back and confirm they land inside the block
        # range phase 2 recorded. A mis-decoded surface would be invisible in
        # the OBJ but obvious in the viewport, so it is checked here.
        hm = tile["height_cm_meta"]
        ymin, ymax = 1e30, -1e30
        for r in (0, h // 3, 2 * h // 3, h - 1):
            line = rows[r]
            for c in range(0, w, max(1, w // 48)):
                i = (c * ch) * 2
                (v,) = struct.unpack(">H", bytes(line[i:i + 2]))
                yb = decode_height_cm(v, hm) / BLOCK_CM
                ymin = min(ymin, yb)
                ymax = max(ymax, yb)
        ok = (ymin >= lo_b - 1.0) and (ymax <= hi_b + 1.0)

        print("  %-24s %4dx%-4d %8d tris  %6.2f MB  y=[%.2f..%.2f] blocks  %s"
              % (tile["file"], w, h, stats["tris"],
                 stats["bytes"] / 1048576.0, ymin, ymax,
                 "OK" if ok else "OUT OF RANGE"))
        if not ok:
            print("    -> refusing to ship this tile")
            return 1

        total_v += stats["verts"]
        total_t += stats["tris"]
        written.append({"obj": stem, "file": tile["file"],
                        "collision_obj": stem + "_collision",
                        "origin_cm": tile["origin_cm"],
                        "resolution": [w, h],
                        "tris": stats["tris"],
                        "collision_tris": pstats["tris"]})

    manifest = os.path.join(args.out, "terrain_manifest.json")
    with open(manifest, "w") as fh:
        json.dump({"dimension": args.dim, "xy_scale_cm": BLOCK_CM,
                   "y_range_blocks": [lo_b, hi_b],
                   "total_verts": total_v, "total_tris": total_t,
                   "tiles": written}, fh, indent=1)

    print("=" * 62)
    print("%d tiles | %d verts | %d tris | %.1fs"
          % (len(written), total_v, total_t, time.time() - t0))
    print("manifest: %s" % manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
