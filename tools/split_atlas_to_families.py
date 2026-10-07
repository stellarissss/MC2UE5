# -*- coding: utf-8 -*-
"""
split_atlas_to_families.py -- cut the calibrated atlas into one tiling texture
per family.

Why this replaces the atlas for surfaces:

The atlas model stretches one cell across each merged quad. Measured, that makes
every 1 m block show an entire 504 px tile, so the average of the tile is what
you see -- a flat colour per block. That is why the campus reads as one flat
tone no matter how the atlas is baked, tinted or lit: the addressing is
correct and the *result* is a voxel-coloured surface. A previous note in the
atlas manifest even states the rule ("a merged quad is STRETCHED onto its
material's cell") without noticing it defeats the purpose at this scale.

Per-family tiling textures with continuous UVs is the standard way architecture
is textured, and the mesh already has one material slot per family: the OBJ
writes `usemtl <family>` per face group and the importer turns each group into a
slot -- verified 13 groups -> 13 slots on bld_001_structure, in the same order.

The source pixels are the atlas cells themselves, so the colour calibration that
was measured and signed off carries over unchanged instead of being redone.

    python3 tools/split_atlas_to_families.py
"""

import json
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_atlas import plan_layout          # noqa: E402

OUT_DIR = "out/families"
ATLAS = "out/atlas/atlas_diffuse.png"
MANIFEST = "out/atlas/manifest.json"

#: Output edge in texels. The atlas cell is 504 px, which is not a power of
#: two; every per-family texture here is sampled with Wrap and put through mip
#: generation and BC compression, and a non-POT source is a whole class of
#: artefacts (mip bleeding, driver-specific padding). 504 -> 512 is a 1.6%
#: LANCZOS resample: visually free, and it makes the texture safe everywhere.
OUT_PX = 512


def main():
    man = json.load(open(MANIFEST))
    fams = list(man["families"].keys()) if isinstance(man["families"], dict) \
        else list(man["families"])
    lay = plan_layout(fams)
    a = lay["_atlas"]
    tile = a["tile"]

    img = Image.open(ATLAS).convert("RGB")
    if img.size != (a["width"], a["height"]):
        print("图集尺寸 %s 与 plan_layout %dx%d 不符 —— 中止"
              % (img.size, a["width"], a["height"]))
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    made = []
    for f in fams:
        x0, y0, x1, y1 = lay[f]["px"]
        cell = img.crop((x0, y0, x1, y1))
        if cell.size != (tile, tile):
            print("  %-10s 尺寸异常 %s" % (f, cell.size))
            continue
        # Edges are made seamless by mirroring a 16 px border inward, so a
        # Tiling > 1 does not show a sharp seam every repeat. Cheap, and it
        # cannot fail silently the way a shader can.
        arr = np.asarray(cell).astype(np.float32)
        w = 16
        arr[:w, :, :] = arr[w:2 * w, :, :][::-1, :, :]
        arr[-w:, :, :] = arr[-2 * w:-w, :, :][::-1, :, :]
        arr[:, :w, :] = arr[:, w:2 * w, :][:, ::-1, :]
        arr[:, -w:, :] = arr[:, -2 * w:-w, :][:, ::-1, :]
        # Resample the mirrored 504 cell up to the POT output size. Done after
        # the mirror so the seamless border is resampled along with the rest
        # and stays continuous.
        resized = Image.fromarray(arr.astype(np.uint8)).resize(
            (OUT_PX, OUT_PX), Image.LANCZOS)
        out = os.path.join(OUT_DIR, "%s.png" % f)
        resized.save(out)
        m = np.asarray(resized).astype(np.float32).reshape(-1, 3).mean(0)
        made.append({"family": f, "png": out, "mean": [round(float(v), 1) for v in m],
                     "r_minus_b": round(float(m[0] - m[2]), 1)})

    with open(os.path.join(OUT_DIR, "manifest.json"), "w") as fh:
        json.dump({"source_atlas": ATLAS, "tile_px": OUT_PX,
                   "source_tile_px": tile, "resample": "LANCZOS",
                   "families": made}, fh, indent=2)

    print("裁剪 %d 个族的平铺贴图 -> %s (%dx%d)"
          % (len(made), OUT_DIR, OUT_PX, OUT_PX))
    print("%-11s %-20s %s" % ("family", "均值", "R-B"))
    for m in sorted(made, key=lambda z: -z["r_minus_b"]):
        print("  %-11s (%6.1f,%6.1f,%6.1f)  %+6.1f"
              % (m["family"], m["mean"][0], m["mean"][1], m["mean"][2],
                 m["r_minus_b"]))
    over = [m["family"] for m in made if m["r_minus_b"] > 10]
    print()
    print("R-B > +10 的族: %s" % (over or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
