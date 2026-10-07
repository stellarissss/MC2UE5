# -*- coding: utf-8 -*-
"""
bake_colour_variants.py -- bake the 21 S5.5 colour variants from the base
per-family textures.

Why bake instead of tint at runtime
-----------------------------------
The variant naming contract already requires one texture per variant
(`T_MC_fabric_lime`), so the per-variant texture cost is paid either way, and
a `Tint` vector parameter in this project has a documented precedent of
"verified set, verified connected, changed zero pixels". Baking writes PNG
bytes, which cannot silently no-op, and can be asserted at the PNG level
*before* the engine opens. Spec: docs/colour_variants.md section 0.

How it works
------------
`gain = target / base_mean` per channel in sRGB, applied as a 3x256 LUT. Two
rules from spec 4.3 are load-bearing and implemented here:

1. **Bake after the resize.** The base textures are already 512x512 (produced
   by `split_atlas_to_families.py`), and the gain is applied to those. Baking
   before the LANCZOS resize would make the realised colour depend on the
   filter, so the gain would no longer hit the target.
2. **The three `brick_*` variants share ONE blurred base.** `brick.png` is
   Gaussian-blurred (radius 12) exactly once and reused, so all terracotta
   variants have the same texture variance -- otherwise the same wall reads as
   different materials. Blurring kills the mortar lines (std 15.7 -> 2.6) which
   is what makes baked brick read as smooth clay terracotta.
3. **Mirror edges come last** (same 16 px mirror the base textures use), so a
   new border introduced by the blur is made seamless again.

The target table lives in `block_families.VARIANT_TARGET` -- the single source
of truth shared with `family_key()`, so the two cannot drift.

    python3 tools/bake_colour_variants.py
"""

import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from block_families import VARIANT_TARGET          # noqa: E402

FAMILIES_DIR = "out/families"
MANIFEST = "out/families/manifest.json"
BLUR_RADIUS = 12          # px; spec 2.1 -- removes mortar, keeps clay mottling
MIRROR = 16               # px; same border size the base textures use


# --------------------------------------------------------------------------- #
# CIEDE2000
# --------------------------------------------------------------------------- #

def _srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def rgb_to_lab(rgb):
    """sRGB 0-255 -> CIE L*a*b* (D65)."""
    lin = _srgb_to_linear(rgb)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ m.T / np.array([0.95047, 1.00000, 1.08883])
    e, k = 216.0 / 24389.0, 24389.0 / 27.0
    f = np.where(xyz > e, np.cbrt(xyz), (k * xyz + 16.0) / 116.0)
    return np.stack([116.0 * f[..., 1] - 16.0,
                     500.0 * (f[..., 0] - f[..., 1]),
                     200.0 * (f[..., 1] - f[..., 2])], axis=-1)


def de2000(rgb1, rgb2):
    """CIEDE2000 between two sRGB triples."""
    lab1, lab2 = rgb_to_lab(rgb1), rgb_to_lab(rgb2)
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar = 0.5 * (C1 + C2)
    G = 0.5 * (1.0 - np.sqrt(Cbar ** 7 / (Cbar ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)

    def hp(b, ap):
        h = np.degrees(np.arctan2(b, ap))
        return h + 360.0 if h < 0 else h

    h1p, h2p = hp(b1, a1p), hp(b2, a2p)
    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180.0:
            dhp -= 360.0
        elif dhp < -180.0:
            dhp += 360.0
    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp) / 2.0)
    Lbarp = 0.5 * (L1 + L2)
    Cbarp = 0.5 * (C1p + C2p)
    if C1p * C2p == 0:
        hbarp = h1p + h2p
    else:
        if abs(h1p - h2p) > 180.0:
            hbarp = 0.5 * (h1p + h2p + 360.0) if (h1p + h2p) < 360.0 \
                else 0.5 * (h1p + h2p - 360.0)
        else:
            hbarp = 0.5 * (h1p + h2p)
    T = (1.0 - 0.17 * np.cos(np.radians(hbarp - 30.0))
         + 0.24 * np.cos(np.radians(2.0 * hbarp))
         + 0.32 * np.cos(np.radians(3.0 * hbarp + 6.0))
         - 0.20 * np.cos(np.radians(4.0 * hbarp - 63.0)))
    dtheta = 30.0 * np.exp(-(((hbarp - 275.0) / 25.0) ** 2))
    Rc = 2.0 * np.sqrt(Cbarp ** 7 / (Cbarp ** 7 + 25.0 ** 7))
    SL = 1.0 + 0.015 * (Lbarp - 50.0) ** 2 / np.sqrt(20.0 + (Lbarp - 50.0) ** 2)
    SC = 1.0 + 0.045 * Cbarp
    SH = 1.0 + 0.015 * Cbarp * T
    RT = -np.sin(np.radians(2.0 * dtheta)) * Rc
    return float(np.sqrt((dLp / SL) ** 2 + (dCp / SC) ** 2 + (dHp / SH) ** 2
                         + RT * (dCp / SC) * (dHp / SH)))


# --------------------------------------------------------------------------- #
# bake
# --------------------------------------------------------------------------- #

def gain_lut(gain):
    """3x256 LUT: sRGB channel value -> clipped round(v * gain)."""
    lut = []
    for g in gain:
        lut += [int(min(255.0, max(0.0, round(v * g)))) for v in range(256)]
    return lut


def mirror_edges(arr, w=MIRROR):
    """Mirror a w-pixel border inward (same trick as split_atlas_to_families).

    Applied last so a border freshly softened by the blur is seamless again.
    """
    arr[:w] = arr[w:2 * w][::-1]
    arr[-w:] = arr[-2 * w:-w][::-1]
    arr[:, :w] = arr[:, w:2 * w][:, ::-1]
    arr[:, -w:] = arr[:, -2 * w:-w][:, ::-1]
    return arr


def base_family(variant_name):
    """'fabric_lime' -> 'fabric' (the variant's base family / source texture)."""
    return variant_name.split("_", 1)[0]


def main():
    want = sorted(VARIANT_TARGET)
    print("烘焙 %d 个颜色变体 -> %s" % (len(want), FAMILIES_DIR))

    # Regression guard: snapshot EVERY pre-existing texture in the families
    # directory (the 22 base families and anything else already there) before
    # anything is written, and re-verify at the end. A bake that overwrote a
    # base texture would be a silent, expensive regression.
    before = {}
    for fn in sorted(os.listdir(FAMILIES_DIR)):
        p = os.path.join(FAMILIES_DIR, fn)
        if fn.endswith(".png") and not fn.startswith("_") \
                and os.path.splitext(fn)[0] not in VARIANT_TARGET:
            before[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    print("基线快照：%d 个既有贴图（含 22 个基础族）" % len(before))

    # Brick's blurred base is built ONCE and shared by all brick_* variants.
    brick_blur = None
    brick_rgb = None
    p = os.path.join(FAMILIES_DIR, "brick.png")
    if os.path.isfile(p):
        brick_rgb = Image.open(p).convert("RGB")
        brick_blur = np.asarray(brick_rgb.filter(
            ImageFilter.GaussianBlur(BLUR_RADIUS))).astype(np.float32)
        print("brick 模糊底（一次，半径 %dpx）: 均值 %s  std %.1f  （原图 std %.1f）"
              % (BLUR_RADIUS, brick_blur.reshape(-1, 3).mean(0).round(1),
                 brick_blur.reshape(-1, 3).std(0).mean(),
                 np.asarray(brick_rgb).astype(np.float32)
                 .reshape(-1, 3).std(0).mean()))

    # Cache each source texture's RGB as float.
    src_cache = {}

    rows = []
    for v in want:
        base = base_family(v)
        target = np.array(VARIANT_TARGET[v], dtype=np.float64)

        if base == "brick":
            src = brick_blur
        else:
            if base not in src_cache:
                q = os.path.join(FAMILIES_DIR, "%s.png" % base)
                src_cache[base] = np.asarray(
                    Image.open(q).convert("RGB")).astype(np.float32)
            src = src_cache[base]

        base_mean = src.reshape(-1, 3).mean(0)
        gain = target / base_mean
        out = np.asarray(Image.fromarray(
            src.astype(np.uint8)).convert("RGB").point(gain_lut(gain)))
        out = mirror_edges(out.copy())

        realized = out.reshape(-1, 3).mean(0).round(3)
        de = de2000(realized, target)

        rgba = np.dstack([out, np.full(out.shape[:2], 255, np.uint8)])
        path = os.path.join(FAMILIES_DIR, "%s.png" % v)
        Image.fromarray(rgba, "RGBA").save(path)

        rows.append({
            "variant": v,
            "base_family": base,
            "source": ("brick blurred r=%d" % BLUR_RADIUS) if base == "brick"
                      else "%s.png" % base,
            "target_rgb": [int(x) for x in target],
            "base_mean": [round(float(x), 2) for x in base_mean],
            "gain": [round(float(x), 4) for x in gain],
            "realized_rgb": [round(float(x), 3) for x in realized],
            "dE2000": round(de, 4),
            "png": path,
        })

    # ---- report + assert ------------------------------------------------
    print()
    print("%-22s %-16s %-16s %-16s %8s" % ("variant", "target", "realized",
                                           "gain", "dE2000"))
    worst = 0.0
    for r in rows:
        worst = max(worst, r["dE2000"])
        print("  %-20s %-16s %-16s %-16s %8.4f"
              % (r["variant"], "(%3d,%3d,%3d)" % tuple(r["target_rgb"]),
                 "(%.1f,%.1f,%.1f)" % tuple(r["realized_rgb"]),
                 "(%.3f,%.3f,%.3f)" % tuple(r["gain"]), r["dE2000"]))
    print("\n最大 dE2000 = %.4f （阈值 < 1.0，目标 ≈ 0.00）" % worst)
    assert worst < 1.0, "a variant missed its target colour: dE=%.3f" % worst

    # 512x512 + exact mirror edges
    bad_size, bad_mirror = [], []
    for r in rows:
        a = np.asarray(Image.open(r["png"]))
        if a.shape[:2] != (512, 512):
            bad_size.append((r["variant"], a.shape))
            continue
        w = MIRROR
        d = [np.abs(a[:w].astype(int) - a[w:2 * w][::-1].astype(int)).max(),
             np.abs(a[-w:].astype(int) - a[-2 * w:-w][::-1].astype(int)).max(),
             np.abs(a[:, :w].astype(int) - a[:, w:2 * w][:, ::-1].astype(int)).max(),
             np.abs(a[:, -w:].astype(int) - a[:, -2 * w:-w][:, ::-1].astype(int)).max()]
        if max(d) != 0:
            bad_mirror.append((r["variant"], d))
    assert not bad_size, bad_size
    assert not bad_mirror, bad_mirror
    print("断言：%d 张变体 PNG 全部 512x512 ✓；16px 镜像边精确（左右/上下 4 边 "
          "逐像素差 = 0）✓" % len(rows))
    # The mirror also makes the *inner* joint (border <-> interior) exactly
    # continuous, which is the discontinuity a naive "stretch the edge texel"
    # approach would show. This is the seam property the mirror actually buys.
    worst_inner = 0.0
    for r in rows:
        a = np.asarray(Image.open(r["png"]).convert("RGB")).astype(int)
        worst_inner = max(worst_inner,
                          np.abs(a[:, MIRROR - 1] - a[:, MIRROR]).max(),
                          np.abs(a[:, -MIRROR - 1] - a[:, -MIRROR]).max(),
                          np.abs(a[MIRROR - 1] - a[MIRROR]).max(),
                          np.abs(a[-MIRROR - 1] - a[-MIRROR]).max())
    print("断言：内侧接缝（边框 <-> 内部，4 处）逐像素差 = %d ✓" % worst_inner)
    assert worst_inner == 0

    # regression: base textures untouched
    after = {p: hashlib.sha256(open(p, "rb").read()).hexdigest() for p in before}
    changed = [p for p in before if before[p] != after[p]]
    assert not changed, "base family textures were modified: %s" % changed
    print("断言：%d 个既有贴图 sha256 烘焙前后未变 ✓（含全部基础族）" % len(before))

    # ---- spec 4.5 extra assertions --------------------------------------
    print("\nspec 4.5 附加断言：")
    by_name = {r["variant"]: r for r in rows}

    def mean_of(v):
        a = np.asarray(Image.open(by_name[v]["png"]).convert("RGB"))
        return a.reshape(-1, 3).mean(0)

    def std_of(v):
        a = np.asarray(Image.open(by_name[v]["png"]).convert("RGB")) \
            .astype(np.float32)
        return float(a.reshape(-1, 3).std(0).mean())

    sep = de2000(mean_of("fabric_lime"), mean_of("fabric_green"))
    print("  fabric_lime vs fabric_green 分离度 dE2000 = %.2f （要求 >= 20）"
          % sep)
    assert sep >= 20.0
    cw = mean_of("concrete_white").mean()
    print("  concrete_white 均值 = %.1f （要求 >= 200）" % cw)
    assert cw >= 200.0
    for v in ("brick_pink", "brick_cyan", "brick_green"):
        s = std_of(v)
        print("  %-12s 纹理 std = %.2f （要求 <= 4）" % (v, s))
        assert s <= 4.0
    for v in ("fabric_lime", "fabric_green"):
        m = mean_of(v)
        print("  %-14s G >= R ? %s  (%s)" % (v, m[1] >= m[0],
                                             tuple(m.round(1))))
        assert m[1] >= m[0]

    # Informational seam numbers. NOTE these are NOT asserted: the wrap-around
    # discontinuity (|col511 - col0|) exceeds the interior adjacent-pixel
    # baseline for a smooth texture, and it does so for the pre-existing base
    # families too -- because the mirror copies the INNER band outward, it makes
    # the border symmetric/inner-continuous but does not make col0 == col511.
    # `import_family_materials.py` sets TA_WRAP, so this is the seam the
    # renderer would actually see; it is a pre-existing property of the base
    # textures (out of scope here) and is reported so it is not mistaken for
    # something the bake introduced.
    print("\n接缝数值（**信息项，未断言**；材质寻址为 TA_WRAP）：")
    for r in rows[:2] + rows[-1:]:
        a = np.asarray(Image.open(r["png"]).convert("RGB")).astype(float)
        print("  %-20s 缠绕缝(左右/上下) %.3f/%.3f  内部基线 %.3f/%.3f"
              % (r["variant"], np.abs(a[:, 0] - a[:, -1]).mean(),
                 np.abs(a[0] - a[-1]).mean(),
                 np.abs(np.diff(a, axis=1)).mean(),
                 np.abs(np.diff(a, axis=0)).mean()))
    if os.path.isfile(os.path.join(FAMILIES_DIR, "brick.png")):
        a = np.asarray(Image.open(os.path.join(FAMILIES_DIR, "brick.png"))
                       .convert("RGB")).astype(float)
        print("  %-20s 缠绕缝(左右/上下) %.3f/%.3f  内部基线 %.3f/%.3f"
              % ("[基线] brick.png", np.abs(a[:, 0] - a[:, -1]).mean(),
                 np.abs(a[0] - a[-1]).mean(),
                 np.abs(np.diff(a, axis=1)).mean(),
                 np.abs(np.diff(a, axis=0)).mean()))


    # merge into the family manifest without disturbing existing fields
    if os.path.isfile(MANIFEST):
        with open(MANIFEST) as fh:
            man = json.load(fh)
    else:
        man = {}
    man["colour_variants"] = {
        "count": len(rows),
        "blur_radius_px": BLUR_RADIUS,
        "mirror_px": MIRROR,
        "max_dE2000": round(worst, 4),
        "targets_from": "block_families.VARIANT_TARGET",
        "items": rows,
    }
    with open(MANIFEST, "w") as fh:
        json.dump(man, fh, indent=2)
    print("\nreport: %s (colour_variants)" % MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
