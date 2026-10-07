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

Alpha
-----
The textures are RGBA. A masked material (BLEND_MASKED) needs an alpha channel
to cut the leaves out; MC leaves are alpha-cutout sprites, not opaque squares.

The atlas is **RGB**, so there is no alpha to carry over -- and the reason is
upstream: `tools/fetch_ambientcg.py` deliberately downloads the `_1K-JPG.zip`
variant (JPG has no alpha), so ambientCG's transparent leaf background was
flattened to **black** before the atlas was ever built. `build_atlas.py` also
`convert("RGB")`s its sources (lines 419 / 564), which would drop alpha anyway.
Measured on `Leaf003_Color.jpg`: 69.45% of pixels are exactly (0,0,0).

So for the families in `ALPHA_KEY` the alpha is *reconstructed* from that black
background (a ramp on the max RGB channel) -- this is the transparency
ambientCG shipped as a PNG and the JPG fetch destroyed. Every other family is
written fully opaque (alpha 255), and if the atlas ever gains a real alpha
channel it is carried through unchanged.

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

#: family -> (lo, hi) max-channel thresholds for reconstructing alpha from a
#: black (lost-transparency) background. max RGB <= lo -> alpha 0 (background),
#: >= hi -> alpha 255 (cutout), linear ramp in between for a soft edge that
#: survives bilinear filtering. Only families whose source actually had a
#: transparent background belong here; everything else stays opaque.
ALPHA_KEY = {
    "leaves": (8, 40),
}


def reconstruct_alpha(rgb, lo, hi):
    """-> uint8 alpha (H, W) keyed off a black background.

    A transparent region that survived a format without an alpha channel is
    not "approximately black", it is exactly (0, 0, 0) (JPEG stored 0 for the
    fully transparent pixels). Keying on the max channel therefore recovers the
    cutout cleanly: leaf pixels are bright in at least one channel, background
    pixels are zero in all three.
    """
    m = rgb.max(axis=2).astype(np.float32)
    a = np.clip((m - lo) / float(hi - lo), 0.0, 1.0) * 255.0
    return a.astype(np.uint8)


def main():
    man = json.load(open(MANIFEST))
    fams = list(man["families"].keys()) if isinstance(man["families"], dict) \
        else list(man["families"])
    lay = plan_layout(fams)
    a = lay["_atlas"]
    tile = a["tile"]

    src = Image.open(ATLAS)
    atlas_has_alpha = src.mode in ("RGBA", "LA")
    img = src.convert("RGBA")
    if img.size != (a["width"], a["height"]):
        print("图集尺寸 %s 与 plan_layout %dx%d 不符 —— 中止"
              % (img.size, a["width"], a["height"]))
        return 1
    print("图集 %s: mode=%s，alpha 通道 %s"
          % (ATLAS, src.mode, "有" if atlas_has_alpha else "无（RGB）"))

    os.makedirs(OUT_DIR, exist_ok=True)
    made = []
    for f in fams:
        x0, y0, x1, y1 = lay[f]["px"]
        cell = img.crop((x0, y0, x1, y1))
        if cell.size != (tile, tile):
            print("  %-10s 尺寸异常 %s" % (f, cell.size))
            continue
        arr = np.asarray(cell).astype(np.float32)          # (H, W, 4)

        # Alpha. Prefer a real atlas alpha; otherwise reconstruct it for the
        # families that lost their transparency to the JPG fetch; otherwise
        # fully opaque.
        keyed = False
        if not atlas_has_alpha and f in ALPHA_KEY:
            lo, hi = ALPHA_KEY[f]
            arr[..., 3] = reconstruct_alpha(arr[..., :3].astype(np.uint8), lo, hi)
            keyed = True

        # Edges are made seamless by mirroring a 16 px border inward, so a
        # Tiling > 1 does not show a sharp seam every repeat. Cheap, and it
        # cannot fail silently the way a shader can. All four channels are
        # mirrored together -- mirroring RGB but not alpha would leave a
        # transparent stripe on the leaf edges.
        w = 16
        arr[:w, :, :] = arr[w:2 * w, :, :][::-1, :, :]
        arr[-w:, :, :] = arr[-2 * w:-w, :, :][::-1, :, :]
        arr[:, :w, :] = arr[:, w:2 * w, :][:, ::-1, :]
        arr[:, -w:, :] = arr[:, -2 * w:-w, :][:, ::-1, :]
        # Resample the mirrored 504 cell up to the POT output size, done after
        # the mirror so the seamless border is resampled with the rest.
        #
        # RGB and alpha are resampled as *separate* images and merged. PIL's
        # LANCZOS is alpha-weighted for RGBA (resampling the same RGB as "RGBA"
        # with alpha=255 is byte-identical, with a keyed alpha it is not), so
        # passing the RGBA image straight in would make the colour depend on
        # the alpha -- a silent coupling. Splitting guarantees the RGB is
        # exactly what the pre-alpha pipeline produced and leaves alpha
        # independent and deterministic.
        rgb_small = Image.fromarray(arr[..., :3].astype(np.uint8), "RGB").resize(
            (OUT_PX, OUT_PX), Image.LANCZOS)
        a_small = Image.fromarray(arr[..., 3].astype(np.uint8), "L").resize(
            (OUT_PX, OUT_PX), Image.LANCZOS)
        resized = Image.merge("RGBA", (*rgb_small.split(), a_small))
        out = os.path.join(OUT_DIR, "%s.png" % f)
        resized.save(out)

        ra = np.asarray(resized)
        m = ra[..., :3].astype(np.float32).reshape(-1, 3).mean(0)
        alpha = ra[..., 3]
        opaque = float((alpha >= 128).mean())
        rec = {"family": f, "png": out, "mode": resized.mode,
               "mean": [round(float(v), 1) for v in m],
               "r_minus_b": round(float(m[0] - m[2]), 1),
               "alpha_opaque_frac": round(opaque, 4)}
        if keyed or not atlas_has_alpha or alpha.min() < 255:
            rec["alpha"] = {
                "min": int(alpha.min()), "max": int(alpha.max()),
                "frac_0": round(float((alpha == 0).mean()), 4),
                "frac_255": round(float((alpha == 255).mean()), 4),
                "source": ("keyed from black background (JPG lost the alpha)"
                           if keyed else "atlas alpha"),
            }
        made.append(rec)

    with open(os.path.join(OUT_DIR, "manifest.json"), "w") as fh:
        json.dump({"source_atlas": ATLAS, "tile_px": OUT_PX,
                   "source_tile_px": tile, "resample": "LANCZOS",
                   "atlas_has_alpha": atlas_has_alpha,
                   "alpha_keyed_families": sorted(ALPHA_KEY),
                   "families": made}, fh, indent=2)

    print("裁剪 %d 个族的平铺贴图 -> %s (%dx%d)"
          % (len(made), OUT_DIR, OUT_PX, OUT_PX))
    modes = {}
    for m in made:
        modes[m.get("mode")] = modes.get(m.get("mode"), 0) + 1
    print("PNG mode 统计: %s" % modes)
    for m in sorted(made, key=lambda z: -z["r_minus_b"]):
        extra = ""
        if m.get("alpha"):
            extra = "  alpha[min=%d max=%d 全透=%.1f%% 全不透=%.1f%%] %s" % (
                m["alpha"]["min"], m["alpha"]["max"],
                100 * m["alpha"]["frac_0"], 100 * m["alpha"]["frac_255"],
                m["alpha"]["source"])
        print("  %-11s (%6.1f,%6.1f,%6.1f)  %+6.1f  不透明像素=%.1f%%%s"
              % (m["family"], m["mean"][0], m["mean"][1], m["mean"][2],
                 m["r_minus_b"], 100 * m["alpha_opaque_frac"], extra))
    over = [m["family"] for m in made if m["r_minus_b"] > 10]
    print()
    print("R-B > +10 的族: %s" % (over or "无"))
    return 0



if __name__ == "__main__":
    sys.exit(main())
