# -*- coding: utf-8 -*-
"""
split_atlas_to_families.py -- produce one tiling texture per material family.

Why per-family textures at all
------------------------------
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

Two source paths
----------------
`--source cc0` (default) builds each texture from the *original CC0 download*
(ambientCG for the re-sourced eight, Poly Haven for the rest), because those
images are authored to tile. `--source atlas` crops the calibrated atlas cell
instead, which is what this script did originally.

The atlas crop cannot tile: the atlas packs a 504 px window out of a 1024 px
periodic source, and 504 is not a period of that source, so the wrap seam is
arbitrary. Measured across the 22 families, the total wrap seam is 302 with the
atlas crop and **208 with the source path (-31%)**, and the number of families
whose wrap seam exceeds their own interior adjacent-pixel distance (i.e. a
visibly mis-placed seam) drops from 5 to **0**. Edge mirroring cannot fix the
crop: it makes the border-to-interior joint continuous but does not make
col0 == col511, and pasting a mirrored band onto a *source-derived* texture
makes it worse again (208 -> 296) by destroying the seam the source had.
Deriving from the source also raises the source resolution used per cell
(1024 -> 512 instead of 504 -> 512).

Either way the per-family colour gain (`build_atlas.BAKE_GAINS`) is applied
*after* the resize, for the same reason `build_atlas.py` documents: baking
before a LANCZOS resize makes the realised colour depend on the filter, so the
gain would no longer hit its target.

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
ambientCG shipped as a PNG and the JPG fetch destroyed. This works identically
on both source paths, because `Leaf003_Color.jpg` is the same black-background
image whether it is cropped from the atlas or read directly. Every other family
is written fully opaque (alpha 255), and if the atlas ever gains a real alpha
channel it is carried through unchanged.

    python3 tools/split_atlas_to_families.py [--source {cc0,atlas}]
"""

import argparse
import json
import os
import sys
import zlib

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_atlas import (ATLAS_DEFAULTS, ATLAS_FAMILIES, GENERATED_RGB,
                         RESOURCE, SOURCE_TILE, SPORT_FAMILY, _find_source,
                         bake, find_ambientcg, plan_layout)          # noqa: E402

OUT_DIR = "out/families"
ATLAS = "out/atlas/atlas_diffuse.png"
MANIFEST = "out/atlas/manifest.json"

#: Where the CC0 downloads live. OUTSIDE the repo (the in-repo
#: assets_cc0/polyhaven is an empty stub), same as `build_atlas.py` defaults.
AMBIENTCG_DIR = "Q:/MC2UE5/assets_cc0/ambientcg"
POLYHAVEN_DIR = "Q:/MC2UE5/assets_cc0/polyhaven"

#: Output edge in texels. The atlas cell is 504 px, which is not a power of
#: two; every per-family texture here is sampled with Wrap and put through mip
#: generation and BC compression, and a non-POT source is a whole class of
#: artefacts (mip bleeding, driver-specific padding). 504 -> 512 is a 1.6%
#: LANCZOS resample: visually free, and it makes the texture safe everywhere.
OUT_PX = 512

#: Mirror border width, matching the base textures' intent.
MIRROR_PX = 16

#: family -> (lo, hi) max-channel thresholds for reconstructing alpha from a
#: black (lost-transparency) background. max RGB <= lo -> alpha 0 (background),
#: >= hi -> alpha 255 (cutout), linear ramp in between for a soft edge that
#: survives bilinear filtering. Only families whose source actually had a
#: transparent background belong here; everything else stays opaque.
ALPHA_KEY = {
    "leaves": (8, 40),
}

#: Families generated procedurally -- no CC0 source and no usable atlas cell.
#: `rgb` is the flat base colour, `grain` the standard deviation of a small
#: seeded noise added so a wall of it is not a mathematically flat colour.
#: KEEP `rgb` IN SYNC with `build_atlas.GENERATED_RGB`; `main()` asserts it.
PROCEDURAL = {
    "glass": {"rgb": (58, 68, 74), "grain": 3.0},
}
GRAIN_SEED = 20260507


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


def mirror_edges(arr, w=MIRROR_PX):
    """Mirror a w-pixel border inward, all channels together.

    Only wanted for an *arbitrary crop*: it makes the border-to-interior joint
    continuous. It cannot make the texture tile (col0 != col511), so it is NOT
    applied to a source-derived texture, whose whole point is that it tiles.
    """
    arr[:w, :, :] = arr[w:2 * w, :, :][::-1, :, :]
    arr[-w:, :, :] = arr[-2 * w:-w, :, :][::-1, :, :]
    arr[:, :w, :] = arr[:, w:2 * w, :][:, ::-1, :]
    arr[:, -w:, :] = arr[:, -2 * w:-w, :][:, ::-1, :]
    return arr


def cc0_source(family, ambientcg_dir, polyhaven_dir):
    """-> (path, label) for a family's CC0 diffuse map, or (None, reason).

    Mirrors `build_atlas.py`'s sourcing exactly: the re-sourced eight come from
    ambientCG (plus `sports`, which reuses the grass map), the generated
    families have no image at all, and everyone else comes from Poly Haven.
    """
    asset_id = RESOURCE.get(family)
    if family == SPORT_FAMILY:
        asset_id = RESOURCE["grass"]
    if asset_id:
        p = find_ambientcg(ambientcg_dir, asset_id)
        if p:
            return p, "ambientCG:%s" % asset_id
        return None, "missing ambientCG %s" % asset_id
    if family in GENERATED_RGB:
        return None, "generated (no image)"
    p, asset = _find_source(polyhaven_dir, family)
    if p:
        return p, "polyhaven:%s" % asset
    return None, "no source found"


def derive_from_source(family, path):
    """-> (H, W, 3) float32 RGB for a family, built from its CC0 source.

    Same order as `build_atlas.py`: normalise to SOURCE_TILE first, then to the
    output size, then bake. Reading the source directly means the output is a
    downscale of a *periodic* image (1024 -> 512 is a clean 2x), so it tiles.
    """
    im = Image.open(path).convert("RGB")
    if im.size != (SOURCE_TILE, SOURCE_TILE):
        im = im.resize((SOURCE_TILE, SOURCE_TILE), Image.LANCZOS)
    im = im.resize((OUT_PX, OUT_PX), Image.LANCZOS)
    return np.asarray(bake(im, family, enabled=True)).astype(np.float32)


def procedural_rgb(family):
    """-> (H, W, 3) float32 for a procedurally generated family.

    The seed is derived through crc32 rather than `hash()`, because `hash()`
    of a str is randomised per process -- the grain would change on every run
    and the texture could not be reproduced or regression-pinned.
    """
    spec = PROCEDURAL[family]
    rng = np.random.default_rng(
        GRAIN_SEED + (zlib.crc32(family.encode("utf-8")) % 100000))
    base = np.zeros((OUT_PX, OUT_PX, 3), dtype=np.float32)
    base += np.array(spec["rgb"], dtype=np.float32)
    if spec.get("grain"):
        base += rng.normal(0.0, spec["grain"], base.shape).astype(np.float32)
    # rint, not truncate: the downstream `astype(np.uint8)` truncates, which
    # would bias a procedural cell about -0.5/channel below its spec colour.
    return np.rint(np.clip(base, 0.0, 255.0))


def seam_metrics(rgb):
    """-> (wrap_h, wrap_v, baseline) on an RGB array, as floats.

    wrap_h / wrap_v are the two wrap-around discontinuities a TA_WRAP sampler
    would show (`|col0-col511|`, `|row0-row511|`); baseline is a same-texture
    interior reference distance (`|col0-col64|`) so the numbers can be read as
    a ratio instead of in isolation.
    """
    a = rgb.astype(np.float32)
    wrap_h = float(np.abs(a[:, 0] - a[:, -1]).mean())
    wrap_v = float(np.abs(a[0] - a[-1]).mean())
    baseline = float(np.abs(a[:, 0] - a[:, 64]).mean())
    return wrap_h, wrap_v, baseline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("cc0", "atlas"), default="cc0",
                    help="cc0 = build from the original CC0 download (tiles); "
                         "atlas = crop the calibrated atlas cell (the old "
                         "path, kept as a fallback / A-B reference)")
    ap.add_argument("--mirror", choices=("auto", "yes", "no"), default="auto",
                    help="auto = mirror the border only on the atlas path")
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--atlas", default=ATLAS)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--ambientcg", default=AMBIENTCG_DIR)
    ap.add_argument("--polyhaven", default=POLYHAVEN_DIR)
    args = ap.parse_args()

    want_mirror = args.mirror == "yes" or (args.mirror == "auto"
                                           and args.source == "atlas")

    # The procedural cells must agree with build_atlas' generated cells, or the
    # atlas cell and the family texture would differ and nobody would notice.
    for f, spec in PROCEDURAL.items():
        if f in GENERATED_RGB and tuple(spec["rgb"]) != tuple(GENERATED_RGB[f]):
            print("FATAL: PROCEDURAL[%s] %s != GENERATED_RGB[%s] %s"
                  % (f, tuple(spec["rgb"]), f, tuple(GENERATED_RGB[f])))
            return 2

    fams = list(ATLAS_FAMILIES)
    lay = plan_layout(fams, **ATLAS_DEFAULTS)
    a = lay["_atlas"]
    tile = a["tile"]

    src = Image.open(args.atlas)
    atlas_has_alpha = src.mode in ("RGBA", "LA")
    atlas_img = src.convert("RGBA")
    if atlas_img.size != (a["width"], a["height"]):
        print("图集尺寸 %s 与 plan_layout %dx%d 不符 —— 中止"
              % (atlas_img.size, a["width"], a["height"]))
        return 1

    print("source=%s  mirror=%s  族数=%d" % (args.source, args.mirror, len(fams)))
    print("图集 %s: mode=%s，alpha 通道 %s"
          % (args.atlas, src.mode, "有" if atlas_has_alpha else "无（RGB）"))
    os.makedirs(args.out_dir, exist_ok=True)

    made = []
    fallback = []
    for f in fams:
        alpha_src = "opaque"
        if f in PROCEDURAL:
            rgb = procedural_rgb(f)
            label = "procedural"
            mirror = False
        elif args.source == "cc0":
            path, label = cc0_source(f, args.ambientcg, args.polyhaven)
            if path:
                rgb = derive_from_source(f, path)
                mirror = want_mirror
            else:
                # No CC0 image: generate, else fall back to the atlas crop.
                if f in GENERATED_RGB:
                    rgb = np.zeros((OUT_PX, OUT_PX, 3), np.float32)
                    rgb += np.array(GENERATED_RGB[f], np.float32)
                    label = "generated %s" % (tuple(GENERATED_RGB[f]),)
                    mirror = False
                else:
                    fallback.append((f, label))
                    rgb, mirror = None, True
        else:
            rgb, label, mirror = None, "atlas crop", want_mirror

        if rgb is None:
            x0, y0, x1, y1 = lay[f]["px"]
            cell = atlas_img.crop((x0, y0, x1, y1))
            if cell.size != (tile, tile):
                print("  %-10s 尺寸异常 %s" % (f, cell.size))
                continue
            arr = np.asarray(cell).astype(np.float32)
            if not atlas_has_alpha and f in ALPHA_KEY:
                lo, hi = ALPHA_KEY[f]
                arr[..., 3] = reconstruct_alpha(arr[..., :3].astype(np.uint8),
                                                lo, hi)
                alpha_src = "keyed from black background (JPG lost the alpha)"
        else:
            alpha = np.full((OUT_PX, OUT_PX), 255, np.uint8)
            if f in ALPHA_KEY:
                lo, hi = ALPHA_KEY[f]
                alpha = reconstruct_alpha(rgb.astype(np.uint8), lo, hi)
                alpha_src = "keyed from black background (JPG lost the alpha)"
            arr = np.dstack([rgb.astype(np.uint8), alpha]).astype(np.float32)

        if mirror:
            arr = mirror_edges(arr)

        # Resize only if the working array is not already at the output size.
        if arr.shape[1] != OUT_PX:
            # RGB and alpha are resampled as *separate* images and merged. PIL's
            # LANCZOS is alpha-weighted for RGBA (resampling the same RGB as
            # "RGBA" with alpha=255 is byte-identical, with a keyed alpha it is
            # not), so passing the RGBA image straight in would make the colour
            # depend on the alpha -- a silent coupling. Splitting guarantees the
            # RGB is exactly what the pre-alpha pipeline produced.
            rgb_small = Image.fromarray(arr[..., :3].astype(np.uint8),
                                        "RGB").resize((OUT_PX, OUT_PX),
                                                      Image.LANCZOS)
            a_small = Image.fromarray(arr[..., 3].astype(np.uint8),
                                      "L").resize((OUT_PX, OUT_PX),
                                                  Image.LANCZOS)
            resized = Image.merge("RGBA", (*rgb_small.split(), a_small))
        else:
            resized = Image.fromarray(arr.astype(np.uint8), "RGBA")

        out = os.path.join(args.out_dir, "%s.png" % f)
        resized.save(out)

        ra = np.asarray(resized)
        m = ra[..., :3].astype(np.float32).reshape(-1, 3).mean(0)
        alpha = ra[..., 3]
        wrap_h, wrap_v, base = seam_metrics(ra[..., :3])
        rec = {"family": f, "png": out, "mode": resized.mode,
               "source_mode": label,
               "mirrored": bool(mirror),
               "mean": [round(float(v), 1) for v in m],
               "r_minus_b": round(float(m[0] - m[2]), 1),
               "alpha_opaque_frac": round(float((alpha >= 128).mean()), 4),
               "seam": {"wrap_h": round(wrap_h, 3), "wrap_v": round(wrap_v, 3),
                        "baseline": round(base, 3),
                        "ratio_h": round(wrap_h / base, 3) if base else None,
                        "ratio_v": round(wrap_v / base, 3) if base else None}}
        if alpha.min() < 255:
            rec["alpha"] = {
                "min": int(alpha.min()), "max": int(alpha.max()),
                "frac_0": round(float((alpha == 0).mean()), 4),
                "frac_255": round(float((alpha == 255).mean()), 4),
                "source": alpha_src,
            }
        made.append((f, rec, out))

    # ---- manifest: merge, never clobber the variant list -----------------
    mpath = os.path.join(args.out_dir, "manifest.json")
    old = {}
    if os.path.isfile(mpath):
        with open(mpath) as fh:
            old = json.load(fh)
    new = dict(old)
    new.update({"source_atlas": args.atlas, "tile_px": OUT_PX,
                "source_tile_px": tile, "resample": "LANCZOS",
                "source_mode": args.source, "mirror": args.mirror,
                "generated_from": (
                    "CC0 downloads (ambientCG + Poly Haven), gap-baked"
                    if args.source == "cc0" else "atlas cells"),
                "atlas_has_alpha": atlas_has_alpha,
                "alpha_keyed_families": sorted(ALPHA_KEY),
                "families": [r for _, r, _ in made]})
    with open(mpath, "w") as fh:
        json.dump(new, fh, indent=2)

    # ---- report ---------------------------------------------------------
    print("写出 %d 个族的平铺贴图 -> %s (%dx%d)"
          % (len(made), args.out_dir, OUT_PX, OUT_PX))
    if fallback:
        print("回退到 atlas 裁切的族: %s" % fallback)
    modes = {}
    for _, r, _ in made:
        modes[r["mode"]] = modes.get(r["mode"], 0) + 1
    print("PNG mode 统计: %s" % modes)
    print()
    print("%-11s %-20s %6s %6s %9s %6s %8s" %
          ("family", "source", "wrapH", "wrapV", "baseline", "ratioH",
           "不透明%"))
    for f, r, _ in sorted(made, key=lambda z: -z[1]["seam"]["wrap_h"]):
        s = r["seam"]
        print("  %-11s %-20s %6.2f %6.2f %9.2f %6.2f %8.1f"
              % (f, r["source_mode"][:20], s["wrap_h"], s["wrap_v"],
                 s["baseline"], s["ratio_h"] or 0,
                 100 * r["alpha_opaque_frac"]))
    wh = [r["seam"]["wrap_h"] for _, r, _ in made]
    rt = [r["seam"]["ratio_h"] for _, r, _ in made if r["seam"]["ratio_h"]]
    print()
    print("wrap_h: 均值 %.2f  中位 %.2f  最大 %.2f（%s）"
          % (np.mean(wh), np.median(wh), np.max(wh),
             [f for f, r, _ in made if r["seam"]["wrap_h"] == np.max(wh)][0]))
    print("ratio_h 中位 %.2f   比值 <1.0 的族: %d/%d"
          % (np.median(rt), sum(1 for x in rt if x < 1.0), len(rt)))
    print("report: %s" % mpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
