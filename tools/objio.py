# -*- coding: utf-8 -*-
"""
objio.py -- the single Wavefront OBJ writer for this pipeline.

Extracted from `build_terrain_mesh.py`, which had the only copy. It was reachable
only through that module's `.u16` + `landscape.json` decoding, so any new caller
had to either reimplement it or fake that format. Two OBJ writers is exactly the
kind of duplication that makes the two drift apart, so the writer is factored out
here and `build_terrain_mesh.write_obj` now delegates to it.

Axis contract (the whole pipeline depends on this one mapping):

    MC block (x, y, z)  ->  UE cm (x * 100, z * 100, y * 100)

Minecraft's ``+z`` becomes Unreal's ``+Y`` and the block height becomes ``Z``.
That is why a heightfield is written with its two grid axes on X/Y and the height
on Z, and why nothing downstream needs to think about it again.
"""

import os

BLOCK_CM = 100.0


def write_heightfield_obj(out_path, z_cm, origin_cm=(0.0, 0.0), block_cm=BLOCK_CM,
                          stride=1, uv_scale=1.0, comment=""):
    """
    Write a regular heightfield as an OBJ, in world centimetres.

    ``z_cm`` is a 2-D array-like of heights **already in centimetres**, indexed
    ``[row, col]`` where ``row`` advances along Unreal Y (Minecraft z) and ``col``
    along Unreal X. Whatever produced it owns the decoding; this function owns the
    geometry.

    ``stride`` > 1 subsamples the grid, for a collision proxy: a full-resolution
    complex-as-simple collision body over hundreds of thousands of triangles cooks
    to a physics mesh far larger than the visual one, while a strided copy traces
    the same at pawn scale.

    ``uv_scale`` multiplies the emitted UVs (which are in block units). One
    texture repeat per N blocks is then ``uv_scale = 1/N``.
    """
    n_rows = len(z_cm)
    n_cols = len(z_cm[0]) if n_rows else 0
    if n_rows < 2 or n_cols < 2:
        raise ValueError("heightfield must be at least 2x2, got %dx%d"
                         % (n_rows, n_cols))

    step = max(1, int(stride))
    rows = list(range(0, n_rows, step))
    if rows[-1] != n_rows - 1:
        rows.append(n_rows - 1)
    cols = list(range(0, n_cols, step))
    if cols[-1] != n_cols - 1:
        cols.append(n_cols - 1)
    gh, gw = len(rows), len(cols)

    ox, oy = float(origin_cm[0]), float(origin_cm[1])

    with open(out_path, "w", newline="\n") as fh:
        w = fh.write
        w("# MC2UE5 heightfield%s\n" % (" (collision proxy)" if step > 1 else ""))
        w("# %d x %d vertices, %d blocks per vertex, XY scale %.1f cm\n"
          % (gw, gh, step, block_cm))
        if comment:
            for line in str(comment).splitlines():
                w("# %s\n" % line)

        for r in rows:
            wy = oy + r * block_cm
            zr = z_cm[r]
            for c in cols:
                w("v %.2f %.2f %.3f\n" % (ox + c * block_cm, wy, zr[c]))

        for r in rows:
            for c in cols:
                w("vt %.5f %.5f\n" % (c * uv_scale, r * uv_scale))

        for _ in rows:
            for _ in cols:
                w("vn 0.0000 0.0000 1.0000\n")

        # Quad (r,c)-(r,c+1)-(r+1,c+1)-(r+1,c) as two triangles, wound CCW seen
        # from above so the face normal is +Z.
        for r in range(gh - 1):
            row0 = r * gw + 1                    # OBJ indices are 1-based
            row1 = (r + 1) * gw + 1
            buf = []
            for c in range(gw - 1):
                a = row0 + c
                b = row0 + c + 1
                cc = row1 + c + 1
                d = row1 + c
                buf.append("f %d/%d/%d %d/%d/%d %d/%d/%d\n"
                           % (a, a, a, cc, cc, cc, b, b, b))
                buf.append("f %d/%d/%d %d/%d/%d %d/%d/%d\n"
                           % (a, a, a, d, d, d, cc, cc, cc))
            w("".join(buf))

    return {"verts": gw * gh, "tris": (gw - 1) * (gh - 1) * 2,
            "bytes": os.path.getsize(out_path)}
