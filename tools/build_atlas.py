# -*- coding: utf-8 -*-
"""
build_atlas.py -- S5: pack the CC0 per-family textures into atlas PNGs.

Why an atlas at all. The 23 Poly Haven families arrive as 23 separate 1024x1024
JPEGs (69 maps, 44 MB). A building mesh draws from a handful of them at once,
and 23 separate samplers means 23 texture binds per draw. One atlas plus one
master material means one sampler, and the per-material slot becomes a UV
offset rather than a shader permutation.

**One atlas cell per material family, and the layout is planned here and
imported by `extract_structures.py`.** The UV contract is the whole fragile
part of an atlas pipeline -- if the mesher and the packer disagree by a texel,
every facade is subtly wrong and nothing errors. So `plan_layout()` is the
single source of truth for cell rectangles, and the mesher calls it rather than
recomputing it.

**UVs stretch onto a tile; they never tile across one.** A cell that repeats
needs its neighbour's texels to bleed into it under mip filtering and bilinear
sampling, which shows up as a colour fringe crawling along a wall at distance.
Stretching costs texel density on large quads, which is why the mesher caps
merged-quad extent; that trade is recorded in the structures manifest rather
than hidden.

Two families have no CC0 source and are generated here, and the manifest
says so explicitly rather than letting them look like sourced art:

    other  a neutral mid-grey, for the handful of blocks that map to no family
    water  a flat blue-green, so the water layer has a cell at all

**Colour correction happens here, in the bake, and nowhere else.** Eight
families are re-sourced from ambientCG (`RESOURCE`) because their previous
sources measured R-B >= +33 at >= 33% saturation, which a per-channel gain
cannot fix: it lifts the mean and leaves the hue ratio, so the cell stays
orange. The rest are corrected by gain alone (`BAKE_GAINS`). The bake is
`Image.point()` with three 256-entry LUTs, applied after the resize and before
the paste. The gains were originally attempted in the shader as a `Tint` vector
parameter, which verified as set, verified as connected, compiled without
error, and changed **zero pixels** -- so nothing in the material graph may be
relied on for colour, and this script asserts the rebuilt PNG cell by cell
before it copies the file anywhere.

Run outside the editor (needs only numpy + Pillow):

    python3 tools/fetch_ambientcg.py     # once: fetch the re-sourced eight
    python3 tools/build_atlas.py
    python3 tools/verify_atlas.py        # independent read-back of the PNG
"""

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from block_families import FAMILIES                        # noqa: E402

#: Atlas families. `block_families.family()` returns one of FAMILIES, "other",
#: or (for water, which no rule matches) "other" as well -- water is forced to
#: its own cell here so the S4 water layer has a real material. `glass` is a
#: real family now (see GLASS_FAMILY) but is generated rather than sourced, so
#: it is kept out of this tuple and appended explicitly below; both are looked
#: up through GENERATED_RGB.
GENERATED_FAMILIES = ("other", "water")

#: The sports-field surface is an *atlas* family, not a *voxel* family: no
#: Minecraft block maps to it yet, so it is deliberately absent from
#: `block_families.FAMILIES`. It is appended last, which is the only safe place
#: to add a family -- inserting it mid-list would shift every subsequent
#: family's index and therefore every UV rect, invalidating every mesh already
#: generated (docs/s5_material_spec.md section 7 risk 10). At index 21 it lands
#: on col 5, row 2, which is the free cell r2c5.
#:
#: Appending it does not change the atlas dimensions: 22 families still need
#: only ceil(22/8) = 3 rows, which rounds up to 4 exactly as 21 did.
SPORT_FAMILY = "sports"

#: The glass family (ART-S5.6). Appended LAST, after `sports`, for the same
#: reason `sports` was: appending cannot shift an existing family's index, and
#: therefore cannot invalidate a mesh already generated. `sports` at index 21
#: and `glass` at 22 keep every cell 0..21 exactly where it was.
#:
#: The commonly quoted "the atlas only has room for 24 families" is wrong: the
#: row count is rounded up to a power of two (`plan_layout`), so 22, 23, 24, 25
#: and up to **32** families all give `rows = 4`. The atlas only grows (and
#: shifts every v coordinate) at the 33rd family. `glass` is therefore free.
GLASS_FAMILY = "glass"

#: The iron-bars family (ART-S5.6). Also appended last. `iron_bars` is 8,161
#: blocks -- 40% of the old `metal` family and its largest single component --
#: and it must be BLEND_MASKED while `iron_block` (3,561) must stay opaque. A
#: per-family material is one blend mode, so it cannot share `metal`.
BARS_FAMILY = "bars"

ATLAS_FAMILIES = (list(FAMILIES) + list(GENERATED_FAMILIES)
                  + [SPORT_FAMILY, GLASS_FAMILY, BARS_FAMILY])

#: Defaults the mesher also reads. Kept in one dict so `--atlas-*` on either
#: tool produces the same numbers.
#:
#: `tile` is 504, not 512, and that is deliberate. `tile + 2*pad` must be a
#: power of two so the atlas as a whole is 4096x1536: mip generation on a
#: non-power-of-two texture is where atlas families cross-contaminate at
#: distance, and 504 is still a multiple of 8 so BC1/BC3 block compression has
#: no wasted edge. `pad` is 4 texels of gutter between cells.
ATLAS_DEFAULTS = {"tile": 504, "cols": 8, "pad": 4}

#: Sourced textures are 1024x1024; the atlas downsamples to `tile`.
SOURCE_TILE = 1024

#: Generated cells. RGB, deliberately desaturated so an unmapped block reads as
#: "neutral material" rather than as a coloured mistake.
GENERATED_RGB = {
    "other": (150, 148, 144),
    "water": (58, 106, 128),
    # ART-S5.6: opaque dark glass. Windows vanish into the white plaster facade
    # (measured dE2000 4.3 between the two cells) because glass was routed to
    # `fabric`; this cell is the cold grey-blue that makes a window read as a
    # window against plaster. Deliberately much greyer than `water` -- glass is
    # not blue water (C* <= 10 vs water's 19.7). The per-family texture adds a
    # little grain on top of this flat colour.
    "glass": (58, 68, 74),
    # ART-S5.6: iron_bars. The atlas cell is the flat rail colour only; the
    # grille cutout lives in the alpha of the per-family texture (an atlas cell
    # cannot hold alpha without turning the whole atlas RGBA).
    "bars": (78, 80, 82),
}

#: Re-sourced families: family -> ambientCG asset id. These eight measured
#: R-B >= +33 with saturation >= 33% in the previous build, which means a
#: per-channel gain only lifts the mean and leaves the hue *ratio* intact --
#: the cell stays orange no matter how large the multiplier. `metal` is the
#: extreme case: it would need a 4.46x blue gain, and a 4.46x lift on a cell
#: whose mean is 61 amplifies its own noise floor into visible blotching.
#:
#: `path` is here too and not because it is warm: `PavingStones103` already
#: measures R-B +8.4, so the fix is picking the cell that is already right. It
#: is in this table because "already right" still has to be fetched.
#:
#: Fetched by tools/fetch_ambientcg.py, which records the resolved ids in
#: out/atlas/ambientcg_sources.json so a rebuild never re-hits the network.
RESOURCE = {
    "grass":  "Grass004",
    "leaves": "Leaf003",
    "bark":   "Bark001",
    "roof":   "RoofingTiles004",
    "brick":  "Bricks074",
    "gravel": "Gravel030",
    "metal":  "MetalPlates006",
    "path":   "PavingStones103",
}

#: Bake gains, family -> (gain_r, gain_g, gain_b), applied through three
#: 256-entry lookup tables via `Image.point()`.
#:
#: This is a **build-time** correction, not a shader one, and that is the whole
#: point. A previous attempt corrected colour with a `Tint` vector parameter on
#: the master material: the parameter verified as set on the instance, the
#: graph node verified as connected to BaseColor, the compile verified as
#: succeeding -- and it changed zero pixels. Nothing in the material graph can
#: be relied on for colour in this project. `Image.point()` is a pure lookup
#: that either changes the bytes or does not, and the assertion at the end of
#: this script reads the written PNG back and checks every cell.
#:
#: Applied AFTER the resize to `tile` and BEFORE `paste`. Before the resize,
#: LANCZOS mixes neighbouring texels and the result depends on the filter, so
#: the realised gain would not be the gain written here.
#:
#: **These gains are solved, not transcribed.** docs/s5_material_spec.md
#: section 2 lists a gain per family and states it was checked as
#: `gain * measured = target`. Neither half of that holds against the files
#: ambientCG actually serves:
#:
#:   * The documented `measured` values do not match the real 1K-JPG colour
#:     maps. Actual/documented runs 0.88 (RoofingTiles004) to 1.68 (Bark001),
#:     so the documented gains would miss by up to 40/255. The spec measured
#:     the API's preview render; this build packs the 1K map.
#:   * `gain * measured = target` ignores the clamp. `Image.point()` saturates
#:     at 255, so any gain above 1.0 on a bright channel lands *below* the
#:     arithmetic. `leaves` is where this bites: its documented green gain of
#:     2.65 is unreachable, because Leaf003 clips to white above about 2.0x
#:     and its green tops out near 79.
#:
#: So tools/calibrate_bake.py solves each channel by bisection on the realised
#: mean of the real pipeline -- open, resize to 1024, resize to 504 LANCZOS,
#: apply the LUT, measure -- and reports the clipped-texel fraction and the
#: standard deviation so a gain cannot quietly delete a cell's texture. Every
#: family below resolves to within 0.4/255 of its target with **0.0% clipping**.
#: Re-run that script after changing any target or source.
#:
#: `brick` deliberately keeps R-B = +50: a brick facade that reaches neutral
#: grey stops being brick, and "the campus stays recognisable" outranks
#: "no warm colours anywhere".
BAKE_GAINS = {
    # --- re-sourced from ambientCG (see RESOURCE) -------------------------
    "grass":  (0.8766, 0.9626, 1.3756),   # Grass004        -> (84, 104, 66)
    "leaves": (1.6595, 1.8264, 1.8468),   # Leaf003         -> (62, 70, 52)
    "bark":   (0.7821, 0.7454, 0.7201),   # Bark001         -> (96, 90, 82)
    "roof":   (1.4575, 1.3657, 1.3521),   # RoofingTiles004 -> (84, 84, 80)
    "brick":  (1.0308, 0.8478, 0.7959),   # Bricks074       -> (132, 94, 82)
    "gravel": (0.9972, 1.0061, 0.9772),   # Gravel030       -> (140, 138, 134)
    "metal":  (1.7424, 1.6833, 1.6974),   # MetalPlates006  -> (120, 122, 125)
    "path":   (0.7550, 0.7688, 0.7751),   # PavingStones103 -> (122, 122, 118)
    # --- kept Poly Haven sources, corrected by gain alone ------------------
    # All R-dominant by a small margin with modest saturation, so 0.9-1.3
    # lands them without reshaping the texture.
    "asphalt":  (0.8810, 0.9870, 1.1160),  # -> (78, 78, 78),   was R-B +18.8
    "soil":     (1.0544, 1.1007, 1.1970),  # -> (104, 90, 74),  was +36.8
    "fabric":   (0.9186, 1.0588, 1.1755),  # -> (178, 180, 184), was +37.1
    "concrete": (1.0571, 1.1364, 1.2556),  # -> (112, 112, 110), was +18.4
    "plaster":  (1.1329, 1.2762, 1.4543),  # -> (178, 178, 174), was +37.7
    "tiles":    (0.9533, 0.9972, 1.0437),  # -> (98, 98, 96),   was +11.0
    "wood":     (0.9745, 0.9669, 0.9656),  # -> (104, 96, 86),  was +17.6
    "rock":     (0.9972, 1.3591, 1.8696),  # -> (96, 94, 92),   was +47.4
    # --- the two neutral greys, kept 6 apart on purpose -------------------
    # Both target ~(104-112) neutral after correction, which is the
    # convergence docs/s5_material_spec.md section 7 risk 7 warns about:
    # `greystone` carries cobblestone/stone and `granite` carries
    # diorite/andesite, and if they read identically the surface loses its
    # material. That is a judgement call for the quality gate, not a
    # threshold -- so the separation is deliberate and documented here, and
    # tools/verify_atlas.py does NOT try to auto-fix it.
    "granite":   (1.4204, 1.4120, 1.4324),  # -> (110, 110, 112)
    "greystone": (1.2759, 1.2300, 1.4317),  # -> (104, 106, 104)
    # --- the courtyard ---------------------------------------------------
    # `quartz` is 31% of the campus surface within 60 blocks of spawn
    # (tools/block_families.py), so it is the highest-leverage cell in the
    # atlas: the single change that most moves the campus off sand.
    "quartz": (1.1079, 1.2657, 1.6498),  # -> (196, 198, 200), was R-B +55.9
    # --- the sports pitch, free cell r2c5, grass source pushed greener ----
    "sports": (1.0049, 1.1283, 1.5404),  # -> (96, 122, 74)
}

#: Expected mean RGB per family after the bake, for the manifest, and the
#: numbers tools/verify_atlas.py asserts the PNG against.
#:
#: These are the spec's targets except `leaves`. The spec's leaves target was
#: (72, 96, 58), reached by a green gain of 2.65 -- but Leaf003 clips to white
#: above about 2.0x, so its green channel tops out near 79 no matter how large
#: the multiplier, and 2.65x would flatten a sixth of the cell to pure white.
#: (62, 70, 52) is the reachable point: measured at a 1.70/1.85/1.85 gain it
#: clips 6% instead of 15%, keeps G-R at +7.9 so the canopy still reads as
#: foliage rather than autumn brown, and holds the cell's standard deviation at
#: 91/103/77 -- the leaf detail the spec chose Leaf003 for in the first place.
#: A brighter green would need a different source, not a bigger multiplier.
EXPECTED_MEAN = {
    "grass": (84, 104, 66), "path": (122, 122, 118), "soil": (104, 90, 74),
    "asphalt": (78, 78, 78), "concrete": (112, 112, 110),
    "plaster": (178, 178, 174), "brick": (132, 94, 82),
    "granite": (110, 110, 112), "tiles": (98, 98, 96), "roof": (84, 84, 80),
    "wood": (104, 96, 86), "bark": (96, 90, 82), "leaves": (62, 70, 52),
    "metal": (120, 122, 125), "gravel": (140, 138, 134), "rock": (96, 94, 92),
    "fabric": (178, 180, 184), "quartz": (196, 198, 200),
    "greystone": (104, 106, 104), "other": (150, 148, 144),
    "water": (58, 106, 128), "sports": (96, 122, 74),
    "glass": (58, 68, 74), "bars": (78, 80, 82),
}

#: Licence. Sourced textures are CC0. Poly Haven supplied the families that
#: are being kept; ambientCG supplied the re-sourced eight and the sports cell.
ATTRIBUTION = {
    "licence": "CC0 1.0 Universal (public domain dedication)",
    "source": "Poly Haven (https://polyhaven.com/textures) for the kept "
              "families; ambientCG (https://ambientcg.com) for the re-sourced "
              "eight and the sports cell",
    "ambientcg_assets": {
        "Grass004": "grass, sports field",
        "Leaf003": "tree foliage",
        "Bark001": "tree bark",
        "PavingStones103": "campus paving (authored 2 m x 2 m)",
        "RoofingTiles004": "roof slate",
        "Bricks074": "facade brick accent",
        "Gravel030": "gravel",
        "MetalPlates006": "metal",
    },
    "maps": "Diffuse only is packed. Roughness and normal maps stay as separate "
            "streaming textures; an atlas cannot hold three channels per cell "
            "without either tripling the size or packing channels into one "
            "atlas, and the master material is a single BaseColor sample.",
}


def plan_layout(families, tile=None, cols=None, pad=None):
    """-> {family: {"index", "col", "row", "uv": [u0, v0, u1, v1], "px": [...]}}

    The single source of truth for where a material lives in the atlas. Cell
    (col, row) occupies pixels [col*C, (col+1)*C) with C = tile + 2*pad, so
    neighbouring cells never share a texel and mip reduction cannot pull one
    family into another.

    UVs have v increasing upward (OpenGL/OBJ convention), so row 0 is at the
    *bottom* of the image and the packing code flips when it writes rows.
    """
    tile = ATLAS_DEFAULTS["tile"] if tile is None else tile
    cols = ATLAS_DEFAULTS["cols"] if cols is None else cols
    pad = ATLAS_DEFAULTS["pad"] if pad is None else pad

    cell = tile + 2 * pad
    used_rows = int(math.ceil(len(families) / float(cols)))
    # Round the row count up to a power of two so the atlas as a whole is POT.
    # A non-POT height (1536, say) forces the texture into a mip path where the
    # reduction filter reaches across cell boundaries, and a wall of one family
    # grows a soft stripe of its neighbour at distance.
    rows = 1
    while rows < used_rows:
        rows *= 2
    width, height = cols * cell, rows * cell

    out = {}
    for i, fam in enumerate(families):
        col, row = i % cols, i // cols
        x0 = col * cell + pad
        # Row 0 is the bottom row in UV space.
        y0 = (rows - 1 - row) * cell + pad
        out[fam] = {
            "index": i,
            "col": col,
            "row": row,
            "px": [x0, y0, x0 + tile, y0 + tile],
            "uv": [x0 / float(width), y0 / float(height),
                   (x0 + tile) / float(width), (y0 + tile) / float(height)],
        }
    out["_atlas"] = {"width": width, "height": height,
                     "rows": rows, "used_rows": used_rows, "cols": cols,
                     "cell": cell,
                     "tile": tile, "pad": pad, "families": list(families)}
    return out


def atlas_size(families, **kw):
    return plan_layout(families, **kw)["_atlas"]


def _find_source(root, family, kind="diffuse"):
    """-> (path, asset_name) for a family's diffuse map, or (None, None)."""
    cands = []
    d = os.path.join(root, family)
    if os.path.isdir(d):
        cands += [os.path.join(d, "%s_%s.jpg" % (family, kind)),
                  os.path.join(d, "%s_%s.png" % (family, kind)),
                  os.path.join(d, "%s_%s.JPG" % (family, kind))]
        for f in sorted(os.listdir(d)):
            if kind in f.lower() and f.lower().endswith((".jpg", ".jpeg",
                                                         ".png")):
                cands.append(os.path.join(d, f))
    for c in cands:
        if os.path.isfile(c):
            return c, family
    return None, None


def find_ambientcg(root, asset_id):
    """-> path to a fetched ambientCG colour map, or None.

    The fetcher writes `<root>/<ASSET_ID>/<ASSET_ID>_Color.jpg`. Accept the
    2K/4K PNG spellings too so a hand-placed download is picked up rather than
    silently falling back to the old Poly Haven source -- a silent fallback here
    would reproduce the `Tint` failure exactly: the build reports success and
    the cell keeps its old colour.
    """
    if not root:
        return None
    d = os.path.join(root, asset_id)
    if not os.path.isdir(d):
        return None
    cands = ["%s_Color.jpg" % asset_id, "%s_color.jpg" % asset_id,
             "%s_Color.png" % asset_id, "%s_color.png" % asset_id]
    for c in cands:
        p = os.path.join(d, c)
        if os.path.isfile(p):
            return p
    for f in sorted(os.listdir(d)):
        low = f.lower()
        if "color" in low and low.endswith((".jpg", ".jpeg", ".png")):
            return os.path.join(d, f)
    return None


def bake(im, fam, enabled=True):
    """Apply a family's per-channel gain through three 256-entry LUTs.

    `Image.point()` with a 768-entry table is a pure lookup: R, G and B each get
    their own 256-entry table, concatenated in channel order. It cannot fail
    quietly. A `Tint` parameter in the material graph was verified as set,
    verified as connected, and changed zero pixels -- this is the opposite of
    that, because the next step reads the pixels back and asserts them.

    Gains are clamped at 255 rather than wrapped, so a gain above 1.0 on a
    bright texel saturates instead of wrapping to black.

    `enabled=False` is the `--no-bake` diagnostic path.
    """
    if not enabled:
        return im
    g = BAKE_GAINS.get(fam)
    if g is None:
        return im
    lut = []
    for gain in g:
        lut += [min(255, int(v * gain)) for v in range(256)]
    return im.point(lut)


def load_source_manifest(root):
    """The fetcher's manifest, if present: family -> Poly Haven asset name."""
    p = os.path.join(root, "manifest.json")
    if not os.path.isfile(p):
        return {}
    with open(p) as fh:
        return json.load(fh)


#: Per-channel tolerance when asserting a rebuilt cell against EXPECTED_MEAN.
#: Generous enough to absorb LANCZOS resampling and the 0.6/255 rounding in the
#: published gains, tight enough that a skipped bake cannot hide inside it --
#: the cells being corrected are 30-100/255 away from their targets.
TOLERANCE = 6

#: Families that must read mineral-neutral after the bake, and the ceiling on
#: abs(R-B). Same list and same number as docs/s5_material_spec.md section 6.
MINERAL_FAMILIES = ("concrete", "granite", "tiles", "gravel", "quartz",
                    "plaster", "asphalt", "metal", "rock", "greystone", "path")
MINERAL_RB_MAX = 12.0

#: Aggregate R-B ceiling over the occupied cells. The campus read as sand at
#: +27.0 before the bake; the spec requires <= +10 after it.
AGGREGATE_RB_MAX = 10.0


def _cell_mean(im, box):
    x0, y0, x1, y1 = box
    crop = im.crop((x0, y0, x1, y1))
    r, g, b = crop.split()
    n = crop.size[0] * crop.size[1]
    return (sum(r.getdata()) / float(n),
            sum(g.getdata()) / float(n),
            sum(b.getdata()) / float(n))


def _verify_saved(png, layout, tol=TOLERANCE):
    """Read `png` back and assert every cell against EXPECTED_MEAN.

    -> {"lines": [...], "failures": [...], "aggregate": (r,g,b), "rb": float}

    Asserts the *pixels*, per cell, plus the aggregate. A per-cell bake dict
    with one typo'd key leaves nineteen families corrected and one untouched,
    which looks like a successful build and renders as one warm cell in an
    otherwise neutral atlas. Only reading the file back catches that.
    """
    from PIL import Image
    im = Image.open(png).convert("RGB")
    lines, failures, means = [], [], []

    for fam in ATLAS_FAMILIES:
        cell = layout[fam]
        mean = _cell_mean(im, cell["px"])
        means.append(mean)
        rb = mean[0] - mean[2]
        want = EXPECTED_MEAN.get(fam)
        bad = []
        if want is not None:
            for ch in range(3):
                if abs(mean[ch] - want[ch]) > tol:
                    bad.append("ch%s %.1f != %d (d=%+.1f, tol %d)"
                               % ("RGB"[ch], mean[ch], want[ch],
                                  mean[ch] - want[ch], tol))
        if fam in MINERAL_FAMILIES and abs(rb) > MINERAL_RB_MAX:
            bad.append("mineral R-B %+.1f outside +/-%.0f" % (rb, MINERAL_RB_MAX))
        if fam in ("grass", "leaves", "sports") and mean[1] < mean[0]:
            bad.append("G %.1f < R %.1f -- hue inversion" % (mean[1], mean[0]))
        if fam == "brick" and not mean[0] > mean[1]:
            bad.append("R %.1f not > G %.1f -- brick stopped being brick"
                       % (mean[0], mean[1]))
        lines.append("r%dc%d %-9s (%5.1f, %5.1f, %5.1f) R-B %+6.1f  %s"
                     % (cell["row"], cell["col"], fam, mean[0], mean[1],
                        mean[2], rb, "ok" if not bad else "FAIL"))
        for b in bad:
            lines.append("        !! " + b)
            failures.append("%s: %s" % (fam, b))

    n = len(means)
    agg = tuple(sum(m[i] for m in means) / n for i in range(3))
    agg_rb = agg[0] - agg[2]
    lines.append("aggregate over %d cells (%5.1f, %5.1f, %5.1f) R-B %+.1f  "
                 "ceiling %+.0f" % (n, agg[0], agg[1], agg[2], agg_rb,
                                   AGGREGATE_RB_MAX))
    if agg_rb > AGGREGATE_RB_MAX:
        failures.append("aggregate R-B %+.1f > %+.0f -- a partial bake"
                        % (agg_rb, AGGREGATE_RB_MAX))
    return {"lines": lines, "failures": failures,
            "aggregate": agg, "aggregate_rb": agg_rb}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets",
                    default="Q:/MC2UE5/assets_cc0/polyhaven",
                    help="CC0 source textures. This path is OUTSIDE the repo; "
                         "the in-repo assets_cc0/polyhaven is an empty stub. "
                         "Recorded in the manifest so S5 is reproducible.")
    ap.add_argument("--ambientcg",
                    default="Q:/MC2UE5/assets_cc0/ambientcg",
                    help="where fetch_ambientcg.py put the re-sourced CC0 "
                         "colour maps. A family in RESOURCE that is missing "
                         "here is a hard error, not a fallback to Poly Haven: "
                         "a silent fallback would ship the old warm cell while "
                         "the build reported success.")
    ap.add_argument("--out", default="out/atlas")
    ap.add_argument("--ue-content", default="project/Content/MC/Atlas",
                    help="where import_chunks.py copies the PNGs before import")
    ap.add_argument("--tile", type=int, default=ATLAS_DEFAULTS["tile"])
    ap.add_argument("--cols", type=int, default=ATLAS_DEFAULTS["cols"])
    ap.add_argument("--pad", type=int, default=ATLAS_DEFAULTS["pad"])
    ap.add_argument("--no-copy", action="store_true",
                    help="skip copying PNGs into the UE Content dir")
    ap.add_argument("--no-bake", action="store_true",
                    help="pack the sources WITHOUT applying BAKE_GAINS. "
                         "Diagnostic only: the result is the pre-bake atlas, "
                         "useful as the 'before' half of a frame A/B test and "
                         "for confirming that a colour change came from the "
                         "bake rather than from a re-source. Never ship it -- "
                         "the assertion below is skipped with it, because the "
                         "whole point of this script is that the artefact is "
                         "checked, and there is nothing to check against.")
    args = ap.parse_args()

    from PIL import Image

    t0 = time.time()
    layout = plan_layout(ATLAS_FAMILIES, tile=args.tile, cols=args.cols,
                         pad=args.pad)
    meta = layout["_atlas"]
    W, H, T, P = meta["width"], meta["height"], meta["tile"], meta["pad"]

    if not os.path.isdir(args.assets):
        print("FATAL: source textures not found at %s" % args.assets)
        print("       pass --assets. The in-repo assets_cc0/polyhaven is an "
              "empty stub, so this cannot fall back to it.")
        return 2

    # Every family the bake is supposed to touch, checked against the dict
    # itself rather than against a hand-copied list. A typo in a BAKE_GAINS key
    # is silent: the family simply ships unbaked and the atlas stays warm at
    # that cell. This is the `Tint` failure mode reproduced inside the bake, so
    # the set of families that *should* be baked is derived from the families
    # that *are* baked and the families that exist, and a difference is fatal.
    want_baked = set(BAKE_GAINS)
    have = set(ATLAS_FAMILIES)
    unknown = want_baked - have
    if unknown:
        print("FATAL: BAKE_GAINS names families that are not in the atlas: %s"
              % ",".join(sorted(unknown)))
        print("       a typo here silently skips a family. Fix the key.")
        return 2

    atlas = Image.new("RGB", (W, H), (0, 0, 0))
    src_manifest = load_source_manifest(args.assets)
    acg_manifest_path = os.path.join(args.out, "ambientcg_sources.json")
    acg_manifest = {}
    if os.path.isfile(acg_manifest_path):
        with open(acg_manifest_path) as fh:
            acg_manifest = json.load(fh).get("assets", {})

    # family -> ambientCG asset id, plus the sports cell which reuses grass.
    acg_for = dict(RESOURCE)
    acg_for[SPORT_FAMILY] = RESOURCE["grass"]

    cells = []
    missing = []
    generated = []
    unsourced = []
    for fam in ATLAS_FAMILIES:
        cell = layout[fam]
        x0, y0, x1, y1 = cell["px"]
        src, asset = None, None
        origin = None
        acg_id = acg_for.get(fam)
        if acg_id:
            src = find_ambientcg(args.ambientcg, acg_id)
            if src is None:
                # Hard error. Falling back to the Poly Haven source here would
                # produce a plausible atlas that is still warm at exactly the
                # cells the re-source was for, and every log line would be fine.
                unsourced.append((fam, acg_id))
                cells.append({"family": fam, "source": None, "asset": acg_id,
                              "pixels": [T, T], "generated": False,
                              "why": "MISSING ambientCG source %s under %s"
                                     % (acg_id, args.ambientcg)})
                continue
            asset, origin = acg_id, "ambientCG"
        else:
            src, asset = _find_source(args.assets, fam)
            origin = "polyhaven" if src else None

        if src:
            im = Image.open(src).convert("RGB")
            if im.size != (SOURCE_TILE, SOURCE_TILE):
                im = im.resize((SOURCE_TILE, SOURCE_TILE), Image.LANCZOS)
            if T != SOURCE_TILE:
                im = im.resize((T, T), Image.LANCZOS)
            # AFTER the resize, BEFORE the paste. Baking before the resize
            # makes the result depend on the LANCZOS filter, so the realised
            # gain would not be the gain written in BAKE_GAINS.
            im = bake(im, fam, enabled=not args.no_bake)
            atlas.paste(im, (x0, y0))
            entry = {"family": fam, "source": src, "asset": asset,
                     "origin": origin, "pixels": [T, T], "generated": False}
            if fam in BAKE_GAINS:
                entry["bake_gain"] = list(BAKE_GAINS[fam])
                entry["expected_mean"] = list(EXPECTED_MEAN[fam])
            a = src_manifest.get(fam)
            if origin == "polyhaven" and isinstance(a, dict) and a.get("asset"):
                entry["asset"] = a["asset"]
                entry["maps"] = a.get("maps")
            if acg_id:
                rec = acg_manifest.get(fam) or acg_manifest.get(
                    acg_id.lower()) or {}
                if rec.get("mean"):
                    entry["source_measured_mean"] = rec["mean"]
            cells.append(entry)
        elif fam in GENERATED_RGB:
            atlas.paste(Image.new("RGB", (T, T), GENERATED_RGB[fam]),
                        (x0, y0))
            generated.append(fam)
            cells.append({"family": fam, "source": None, "asset": None,
                          "pixels": [T, T], "generated": True,
                          "rgb": list(GENERATED_RGB[fam]),
                          "expected_mean": list(GENERATED_RGB[fam]),
                          "why": "no CC0 source for this family in the campus "
                                 "palette; generated so the mesh has a cell"})
        else:
            missing.append(fam)
            cells.append({"family": fam, "source": None, "asset": None,
                          "pixels": [T, T], "generated": False,
                          "why": "MISSING"})

    if unsourced:
        print("FATAL: %d re-sourced famil(y/ies) have no ambientCG download:"
              % len(unsourced))
        for fam, asset_id in unsourced:
            print("       %-9s wants %s under %s" % (fam, asset_id,
                                                     args.ambientcg))
        print("       Run tools/fetch_ambientcg.py. Refusing to fall back to")
        print("       Poly Haven: that would ship the old warm cell at exactly")
        print("       the families the re-source exists to fix, and every log")
        print("       line here would be green.")
        return 2

    os.makedirs(args.out, exist_ok=True)
    png = os.path.join(args.out, "atlas_diffuse.png")
    atlas.save(png, optimize=True)

    # Read the saved PNG back and assert the per-cell means BEFORE handing it
    # to anyone. This is the whole lesson of the `Tint` failure, applied to the
    # bake: the bake is a per-cell dict lookup, so a partially-applied bake
    # produces a plausible atlas with some cells corrected and some not, and
    # nothing errors. Asserting the artefact catches that here rather than
    # three steps downstream in a screenshot.
    #
    # The check is on the file that was just written, not on the in-memory
    # Image, so a save that silently wrote something else is also caught.
    if args.no_bake:
        print("WARNING --no-bake: packing sources with no colour correction. "
              "This is the pre-bake atlas; do not ship it.")
    else:
        verify = _verify_saved(png, layout, TOLERANCE)
        for line in verify["lines"]:
            print("  " + line)
        if verify["failures"]:
            print()
            print("FATAL: the rebuilt atlas does not match its targets "
                  "(%d assertion(s)):" % len(verify["failures"]))
            for f in verify["failures"]:
                print("       %s" % f)
            print("       The PNG has been left on disk for inspection, but "
                  "it must not be shipped. Fix the gains or the source.")
            return 1

    copied = []
    if not args.no_copy:
        for dst in (args.ue_content, os.path.join(args.out, "for_ue")):
            os.makedirs(dst, exist_ok=True)
            import shutil
            d = os.path.join(dst, os.path.basename(png))
            shutil.copyfile(png, d)
            copied.append(d)

    # Textures whose cells are addressed by the mesher must be exactly the
    # layout the mesher planned. Assert it rather than trust two literals.
    for fam, cell in layout.items():
        if fam.startswith("_"):
            continue
        px = cell["px"]
        assert px[2] - px[0] == T and px[3] - px[1] == T, fam
        assert 0 <= px[0] and px[2] <= W and 0 <= px[1] and px[3] <= H, fam

    manifest = {
        "step": "S5",
        "atlas": {"width": W, "height": H, "rows": meta["rows"],
                  "cols": meta["cols"], "cell": meta["cell"],
                  "tile_px": T, "pad_px": P,
                  "png": os.path.relpath(png).replace("\\", "/"),
                  "bytes": os.path.getsize(png),
                  "v_convention": "UV v increases upward; row 0 is the bottom "
                                  "row of the image"},
        "uv_contract": {
            "rule": "a merged quad is STRETCHED onto its material's cell, "
                    "never tiled across it",
            "why": "a repeating cell bleeds its neighbour's texels under mip "
                   "reduction and bilinear filtering",
            "cost": "texel density falls on large quads; extract_structures.py "
                    "caps merged-quad extent (--max-quad) to bound this",
            "planned_by": "plan_layout() in this file; extract_structures.py "
                          "imports it rather than re-deriving",
        },
        "families": {k: {"index": v["index"], "col": v["col"], "row": v["row"],
                         "uv": v["uv"], "px": v["px"]}
                     for k, v in layout.items() if not k.startswith("_")},
        "cells": cells,
        "generated_families": generated,
        "missing_families": missing,
        "bake": {
            "mechanism": "Image.point() with three 256-entry LUTs, applied "
                         "after the resize to tile and before the paste",
            "why_not_a_shader_parameter": "an in-shader 'Tint' vector "
                                          "parameter verified as set on the "
                                          "instance, verified as connected to "
                                          "BaseColor, compiled cleanly, and "
                                          "changed zero pixels. Colour "
                                          "correction lives in the bake, never "
                                          "in the material graph.",
            "gains": {k: list(v) for k, v in sorted(BAKE_GAINS.items())},
            "expected_mean": {k: list(v)
                              for k, v in sorted(EXPECTED_MEAN.items())},
            "tolerance_per_channel": TOLERANCE,
            "verified_on_rebuild": not args.no_bake,
            "measured_aggregate_rgb": ([round(v, 1) for v in verify["aggregate"]]
                                       if not args.no_bake else None),
            "measured_aggregate_rb": (round(verify["aggregate_rb"], 1)
                                      if not args.no_bake else None),
            "aggregate_rb_ceiling": AGGREGATE_RB_MAX,
            "note": "asserted by reading the written PNG back, per cell, "
                    "before it was copied to the UE Content dir",
        },
        "resourced_families": dict(sorted(RESOURCE.items())),
        "source": {
            "root": args.assets,
            "ambientcg_root": args.ambientcg,
            "outside_repo": True,
            "in_repo_copy": "assets_cc0/polyhaven (empty stub; NOT used)",
            "families_found": len([c for c in cells if c["source"]]),
            "jpeg_files": sum(len([f for f in os.listdir(
                os.path.join(args.assets, f))
                if f.lower().endswith((".jpg", ".jpeg"))])
                for f in os.listdir(args.assets)
                if os.path.isdir(os.path.join(args.assets, f))),
        },
        "attribution": ATTRIBUTION,
        "ue_copies": copied,
        "elapsed_s": round(time.time() - t0, 1),
    }
    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    print("atlas: %dx%d  %d cells (%d cols x %d rows), tile %d, pad %d"
          % (W, H, len(ATLAS_FAMILIES), meta["cols"], meta["rows"], T, P))
    print("  %d sourced families, %d generated (%s), %d missing"
          % (len([c for c in cells if c["source"]]), len(generated),
             ",".join(generated) or "-", len(missing)))
    if missing:
        print("  MISSING: %s" % ",".join(missing))
    print("  png %s (%.1f MB) in %.1fs"
          % (os.path.relpath(png).replace("\\", "/"),
             os.path.getsize(png) / 1048576.0, time.time() - t0))
    print("report: %s/manifest.json" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())