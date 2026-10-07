# -*- coding: utf-8 -*-
"""
reference_map.py -- top-down campus map coloured by the REAL Minecraft colour
of each column's top block.

Why this exists
---------------
The core project constraint is "the campus must correspond to the save", and
until now there was no ruler for it. The two existing overhead views cannot
serve: `render_topdown.py` is a *height* render (hypsometric tint + hillshade)
so its colours encode elevation, not material, and `campus_map.py` is an ASCII
grid with its own private material taxonomy. Neither answers "what colour is
the ground at (x, z) in Minecraft".

This tool answers exactly that: one pixel per block column, coloured with the
canonical Minecraft colour of the topmost non-empty block. It is both

  1. the **acceptance truth** for "the campus matches the save" -- compare it
     against a UE screenshot and the pitch / gates / each building must line up
     in both position and colour; and
  2. the **target colour reference** for the S5.5 colour-variant work, so the
     ΔE comparison is numeric instead of eyeballed.

Material classification is derived from `block_families.py` (the single
authoritative mapping). Only the *colour* table below is new, and it is
deliberately explicit and commented so it can be audited.

Outputs (all under out/):
  ref_map.png        448x768, 1 block = 1 px, no legend   (the measured ruler)
  ref_map_4x.png     nearest-neighbour 4x of a legend band + the map, to read
  ref_map_legend.json [{rgb, colour_name, block_names, blocks, share}]
  ref_map_stats.txt  every (colour, block) pair by block count, not truncated

    python3 tools/reference_map.py
"""

import json
import os
import sys
from collections import Counter

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from block_families import FAMILIES, family                 # noqa: E402

CAMPUS = (-144, 303, -544, 223)      # x0, x1, z0, z1  -> 448 x 768 columns
MATERIAL_CACHE = "out/materials/voxelmat.npz"
OUT_DIR = "out"

# --------------------------------------------------------------------------- #
# colour tables
# --------------------------------------------------------------------------- #

#: Minecraft's 16 official dye colours, RGB 0-255. These are the values the
#: game ships as the dye item colours and are the base for wool / concrete /
#: carpet / stained glass, so those blocks need no per-block entry.
MC_DYE_COLORS = {
    "white":      (249, 255, 254),
    "orange":     (240, 118,  19),
    "magenta":    (199,  78, 189),
    "light_blue": ( 58, 175, 217),
    "yellow":     (254, 216,  61),
    "lime":       (128, 199,  31),
    "pink":       (243, 139, 170),
    "gray":       ( 66,  70,  73),
    "light_gray": (157, 157, 151),
    "cyan":       ( 21, 137, 145),
    "purple":     (121,  42, 172),
    "blue":       ( 53,  57, 157),
    "brown":      (114,  71,  40),
    "green":      ( 94, 124,  22),
    "red":        (176,  46,  38),
    "black":      ( 29,  29,  33),
}

#: Blocks whose colour is their colour-prefix dye colour. Tested after the
#: explicit table, longest suffix first so `_stained_glass_pane` wins over
#: `_stained_glass`.
DYE_SUFFIXES = ("_stained_glass_pane", "_stained_glass", "_concrete",
                "_wool", "_carpet")

#: Per-species colours for leaves / logs / planks. The values are the average
#: colour of the vanilla texture, which is what the map should show -- the item
#: icon is not representative (leaves are far darker in world than on the
#: inventory icon).
LEAF_COLORS = {
    "oak":      ( 60, 143,  55),
    "birch":    (100, 158,  86),
    "spruce":   ( 42,  90,  54),
    "jungle":   ( 48, 150,  29),
    "acacia":   (119, 148,  42),
    "dark_oak": ( 48, 109,  42),
}
LOG_COLORS = {
    "oak":      (109,  85,  50),
    "birch":    (196, 180, 123),
    "spruce":   ( 59,  38,  18),
    "jungle":   ( 85,  67,  25),
    "acacia":   (168,  90,  50),
    "dark_oak": ( 60,  46,  26),
}

#: Suffixes stripped to look up a base material's colour (brick_slab ->
#: bricks, and so on). The base is then looked up in BLOCK_COLORS.
STRIP_SUFFIXES = ("_pressure_plate", "_wall_sign", "_trapdoor", "_stairs",
                  "_slab", "_sign", "_fence", "_gate", "_door", "_wall",
                  "_button")

#: Explicit per-block colours. Sources are noted per group. Where a value is
#: the Minecraft **map colour** (the colour the in-game map renders a block as)
#: it is exact and official; where it is a texture average it is a measured,
#: hand-checked approximation of the vanilla texture.
BLOCK_COLORS = {
    # ---- ground / terrain -------------------------------------------------
    "grass_block":            (127, 178,  56),   # MC map colour (grass)
    "grass":                  (127, 178,  56),   # short grass plant reads as grass
    "tall_grass":             (127, 178,  56),
    "fern":                   ( 94, 124,  22),   # green dye, darker planting
    "large_fern":             ( 94, 124,  22),
    "dirt":                   (134,  96,  67),   # MC map colour
    "coarse_dirt":            (134,  96,  67),
    "podzol":                 (129,  86,  49),   # MC map colour
    "sand":                   (219, 207, 163),   # MC map colour
    "sandstone":              (216, 203, 155),   # MC map colour
    "smooth_sandstone":       (216, 203, 155),
    "smooth_sandstone_slab":  (216, 203, 155),
    "smooth_sandstone_stairs":(216, 203, 155),
    # ---- masonry ----------------------------------------------------------
    "cobblestone":            (115, 115, 115),   # MC map colour (cobblestone)
    "cobblestone_slab":       (115, 115, 115),
    "stone":                  (125, 125, 125),   # MC map colour
    "stone_slab":             (125, 125, 125),
    "stone_stairs":           (125, 125, 125),
    "stone_button":           (125, 125, 125),
    "stone_pressure_plate":   (125, 125, 125),
    "stone_bricks":           (122, 122, 122),
    "stone_brick_slab":       (122, 122, 122),
    "stone_brick_stairs":     (122, 122, 122),
    "smooth_stone":           (158, 158, 158),   # MC map colour (smooth stone)
    "smooth_stone_slab":      (158, 158, 158),
    "bricks":                 (150,  97,  83),   # MC map colour
    "brick_slab":             (150,  97,  83),
    "brick_stairs":           (150,  97,  83),
    "end_stone":              (219, 222, 158),   # MC map colour
    "end_stone_bricks":       (219, 222, 158),
    "end_stone_brick_slab":   (219, 222, 158),
    "end_stone_brick_stairs": (219, 222, 158),
    "quartz_block":           (235, 229, 222),   # MC map colour (near-white)
    "quartz_slab":            (235, 229, 222),
    "quartz_stairs":          (235, 229, 222),
    "smooth_quartz":          (235, 229, 222),
    "smooth_quartz_slab":     (235, 229, 222),
    "smooth_quartz_stairs":   (235, 229, 222),
    "chiseled_quartz_block":  (235, 229, 222),
    "lapis_block":            ( 30,  67, 140),   # MC map colour
    "obsidian":               ( 21,  18,  30),   # MC map colour
    "prismarine_brick_slab":  ( 99, 171, 158),   # MC map colour (prismarine)
    "polished_granite_slab":  (149, 103,  85),   # MC map colour (granite)
    "diorite_wall":           (188, 188, 190),   # MC map colour
    "diorite_slab":           (188, 188, 190),
    "polished_andesite_stairs":(136, 136, 136),  # MC map colour (andesite)
    "terracotta":             (152,  94,  67),   # MC map colour (terracotta)
    # Pink terracotta is the running-track surface: MC map colour for the
    # coloured terracotta, a muted brick red -- NOT the bright pink dye.
    "pink_terracotta":        (161,  83,  78),
    "red_nether_brick_stairs":( 44,  22,  26),   # MC map colour (nether brick)
    "red_nether_brick_slab":  ( 44,  22,  26),
    "red_nether_brick_wall":  ( 44,  22,  26),
    # ---- wood -------------------------------------------------------------
    "crimson_fence":          (101,  48,  70),   # crimson planks average
    "dark_oak_stairs":        ( 66,  43,  20),   # dark oak planks
    "jungle_stairs":          (160, 115,  80),   # jungle planks
    "acacia_fence":           (168,  90,  50),   # acacia planks
    "composter":              (110,  80,  40),
    "campfire":               (120,  80,  40),
    "scaffolding":            (168, 150,  90),   # bamboo
    "oak_sign":               (160, 130,  78),   # oak planks
    "oak_wall_sign":          (160, 130,  78),
    "spruce_trapdoor":        ( 59,  38,  18),   # spruce planks
    "birch_trapdoor":         (196, 180, 123),   # birch planks
    "birch_slab":             (196, 180, 123),
    "birch_stairs":           (196, 180, 123),
    # ---- metal / fixtures -------------------------------------------------
    "iron_bars":              (140, 142, 140),   # MC map colour (iron)
    "iron_door":              (200, 200, 200),
    "anvil":                  ( 74,  74,  76),
    "cauldron":               ( 70,  70,  72),
    "lantern":                (150, 120,  60),
    "diamond_block":          ( 92, 219, 213),   # MC map colour
    "lodestone":              (150, 150, 152),
    "beacon":                 (110, 220, 220),
    "lever":                  (110, 110, 110),
    "dispenser":              (108, 108, 108),
    "observer":               ( 90,  90,  90),
    "heavy_weighted_pressure_plate": (125, 125, 125),
    # ---- glass / misc -----------------------------------------------------
    "glass":                  (200, 220, 230),   # pale glass, not dye
    "glass_pane":             (200, 220, 230),
    "cobweb":                 (230, 230, 230),
    "vine":                   ( 50, 110,  40),
    "shroomlight":            (250, 160,  80),
    "spawner":                ( 30,  40,  35),
    "tripwire":               (150, 150, 150),
    "white_wall_banner":      (240, 240, 240),
    # ---- flowers ----------------------------------------------------------
    "poppy":                  (216,  60,  60),   # MC map colour (red flower)
    "red_tulip":              (216,  60,  60),
    "dandelion":              (255, 236,  40),   # MC map colour (yellow flower)
    "azure_bluet":            (220, 220, 230),
    "cornflower":             ( 70,  90, 200),   # MC map colour (blue flower)
    "oxeye_daisy":            (240, 240, 220),
    "pink_tulip":             (243, 139, 170),
    "white_tulip":            (249, 255, 254),
    "orange_tulip":           (240, 118,  19),
}

#: Fallback when a block is neither in the table nor a known colour/wood
#: variant: the representative colour of its `block_families` family. Covers
#: `other` and `water` too, which are not in FAMILIES.
FAMILY_COLORS = {
    "grass":     (127, 178,  56),
    "path":      (140, 120,  80),
    "soil":      (134,  96,  67),
    "asphalt":   ( 90,  90,  92),
    "concrete":  (180, 180, 180),
    "plaster":   (200, 196, 190),
    "brick":     (150,  97,  83),
    "granite":   (149, 103,  85),
    "tiles":     (150, 120,  90),
    "roof":      (110,  80,  70),
    "wood":      (160, 130,  78),
    "bark":      (109,  85,  50),
    "leaves":    ( 60, 143,  55),
    "metal":     (140, 142, 140),
    "gravel":    (127, 124, 123),
    "rock":      (110, 110, 112),
    "fabric":    (200, 200, 200),
    "quartz":    (235, 229, 222),
    "greystone": (127, 127, 127),
    "other":     (150, 148, 144),
    "water":     ( 58, 106, 128),
    "sports":    (110, 190,  60),
}
FAMILY_FALLBACK = FAMILY_COLORS["other"]

#: Colour of an all-air column. None exist in this save (all 344,064 columns
#: have a solid block) but the code must not silently paint names[0].
EMPTY_COLOR = (0, 0, 0)


def block_rgb(name):
    """-> ((r, g, b), source) for a namespaced block name.

    ``source`` records how the colour was resolved ("block" / "dye" /
    "species" / "strip" / "family") so an audit can tell a curated value from
    a fallback.
    """
    n = name.split(":", 1)[-1]
    if n in BLOCK_COLORS:
        return BLOCK_COLORS[n], "block"
    for suf in DYE_SUFFIXES:
        if n.endswith(suf):
            stem = n[:-len(suf)]
            if stem in MC_DYE_COLORS:
                return MC_DYE_COLORS[stem], "dye"
    for suf, tbl in (("_leaves", LEAF_COLORS), ("_log", LOG_COLORS),
                     ("_wood", LOG_COLORS), ("_planks", LOG_COLORS)):
        if n.endswith(suf):
            sp = n[:-len(suf)]
            if sp in tbl:
                return tbl[sp], "species"
    if n.endswith("_sapling"):
        return LEAF_COLORS["oak"], "species"
    for suf in STRIP_SUFFIXES:
        if n.endswith(suf):
            base = n[:-len(suf)]
            if base in BLOCK_COLORS:
                return BLOCK_COLORS[base], "strip"
            if base + "s" in BLOCK_COLORS:          # brick -> bricks
                return BLOCK_COLORS[base + "s"], "strip"
    return FAMILY_COLORS.get(family(name), FAMILY_FALLBACK), "family"


# --------------------------------------------------------------------------- #
# top block per column
# --------------------------------------------------------------------------- #

def top_block_per_column():
    """-> (name_index (W, D) uint16, has_block (W, D) bool).

    The algorithm is `classify.py`'s: walk y from 0 up and keep the last
    non-empty voxel, i.e. each column's topmost solid block. Solid is
    ``family != 0`` in the material cache because family slot 0 is reserved
    for air (real families are 1..N), which is the same "not EMPTY" test
    classify uses. Reading the cache avoids re-scanning the multi-GB save.
    """
    z = np.load(MATERIAL_CACHE, allow_pickle=False)
    fam, nid = z["family"], z["nameid"]
    W, H, D = fam.shape
    top = np.zeros((W, D), dtype=nid.dtype)
    has = np.zeros((W, D), bool)
    for y in range(H):
        here = fam[:, y, :] != 0
        top = np.where(here, nid[:, y, :], top)
        has |= here
    return top, has


def build_map(names):
    """-> (rgb_img (D, W, 3) uint8, top_name (D, W) str, has (D, W) bool).

    Image axes: row = z (top row is z0 = z min), column = x. That is the
    natural screen orientation -- +x to the right, +z downward.
    """
    top, has = top_block_per_column()
    W = top.shape[0]
    # Vectorise the name lookup: precompute the colour of every palette entry.
    palette_rgb = np.array([block_rgb(nm)[0] for nm in names], dtype=np.uint8)
    rgb = palette_rgb[top]                       # (W, D, 3)
    rgb = np.where(has[..., None], rgb, np.array(EMPTY_COLOR, np.uint8))
    img = np.transpose(rgb, (1, 0, 2))           # (D, W, 3)
    name_arr = np.array(names, dtype=object)[top]
    name_arr = np.transpose(name_arr, (1, 0))
    has_t = np.transpose(has, (1, 0))
    return img, name_arr, has_t


def main():
    z = np.load(MATERIAL_CACHE, allow_pickle=False)
    names = [str(s) for s in z["names"]]
    img, top_name, has = build_map(names)
    H, W = top_name.shape
    print("map %dx%d (x %d..%d, z %d..%d), top block per column"
          % (W, H, CAMPUS[0], CAMPUS[1], CAMPUS[2], CAMPUS[3]))
    print("columns with no solid block: %d" % int((~has).sum()))

    # ---- counts: per (colour, block) and per colour --------------------
    pair = Counter()
    per_colour = Counter()
    colour_names = {}
    for j in range(H):
        for i in range(W):
            if not has[j, i]:
                continue
            nm = top_name[j, i]
            rgb = tuple(int(v) for v in img[j, i])
            pair[(rgb, nm)] += 1
            per_colour[rgb] += 1
    total = int(has.sum())
    print("distinct colours: %d   distinct (colour, block) pairs: %d"
          % (len(per_colour), len(pair)))

    # name for each colour = its most common block, plus all block names.
    blocks_by_colour = {}
    for (rgb, nm), c in pair.items():
        blocks_by_colour.setdefault(rgb, []).append((nm, c))
    for rgb, lst in blocks_by_colour.items():
        lst.sort(key=lambda t: (-t[1], t[0]))
        colour_names[rgb] = lst[0][0]

    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- ref_map.png : exactly 448x768, no legend ----------------------
    ref_path = os.path.join(OUT_DIR, "ref_map.png")
    Image.fromarray(img).save(ref_path)

    # ---- legend --------------------------------------------------------
    ranked = sorted(per_colour.items(), key=lambda t: (-t[1], t[0]))
    legend = []
    for rgb, c in ranked:
        legend.append({
            "rgb": list(rgb),
            "colour_name": colour_names[rgb],
            "block_names": [nm for nm, _ in blocks_by_colour[rgb]],
            "blocks": int(c),
            "share": round(c / float(total), 6),
        })
    with open(os.path.join(OUT_DIR, "ref_map_legend.json"), "w") as fh:
        json.dump(legend, fh, indent=2, ensure_ascii=False)

    # ---- ref_map_stats.txt : every (colour, block) pair, by count ------
    stats_path = os.path.join(OUT_DIR, "ref_map_stats.txt")
    with open(stats_path, "w", newline="\n") as fh:
        fh.write("# MC2UE5 reference map -- top block per column\n")
        fh.write("# campus x[%d..%d] z[%d..%d], %d columns, %d distinct "
                 "colours, %d (colour, block) pairs\n"
                 % (CAMPUS[0], CAMPUS[1], CAMPUS[2], CAMPUS[3], total,
                    len(per_colour), len(pair)))
        fh.write("# %-16s %9s %8s   %s\n" % ("rgb", "blocks", "share", "block"))
        for (rgb, nm), c in sorted(pair.items(), key=lambda t: (-t[1], t[0][1])):
            fh.write("  %-16s %9d %7.3f%%   %s\n"
                     % ("(%d,%d,%d)" % rgb, c, 100.0 * c / total, nm))
    stats_lines = sum(1 for _ in open(stats_path))
    print("stats lines (incl. 3 header lines): %d" % stats_lines)

    # ---- ref_map_4x.png : legend band on top + 4x nearest-neighbour ----
    sw = 8                                            # swatch width, 1x px
    per_row = max(1, W // sw)
    rows = (len(legend) + per_row - 1) // per_row
    band = np.full((rows * sw, W, 3), 24, dtype=np.uint8)   # dark background
    for idx, entry in enumerate(legend):
        r, c = divmod(idx, per_row)
        band[r * sw:(r + 1) * sw, c * sw:(c + 1) * sw] = entry["rgb"]
    combined = np.vstack([band, img])
    big = Image.fromarray(combined).resize(
        (W * 4, combined.shape[0] * 4), Image.NEAREST)
    big_path = os.path.join(OUT_DIR, "ref_map_4x.png")
    big.save(big_path)

    # ---- headline numbers (the sports field the lead asked about) ------
    print("\nsports-field colours:")
    for key in ("lime_wool", "green_wool", "pink_terracotta"):
        c = sum(cnt for (rgb, nm), cnt in pair.items()
                if nm.split(":", 1)[-1] == key)
        print("  %-16s %8d  %6.3f%%" % (key, c, 100.0 * c / total))
    print("\nwrote: %s  %s  %s  %s"
          % (ref_path, big_path, os.path.join(OUT_DIR, "ref_map_legend.json"),
             stats_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
