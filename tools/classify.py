# -*- coding: utf-8 -*-
"""
classify.py -- split the campus voxels into terrain / structure / vegetation /
water / detail.

This is step S1 of the academic-route rebuild (see REFACTOR_PLAN.md): the
SIGGRAPH '25 "Minecraft to 3D" pipeline starts by deciding *what is terrain and
what is an object*, and every later stage depends on that split. The paper does
it with a 3D U-Net; the weights are not published, so this does it with rules
over block names and local surface geometry.

Why rules can work here. The paper needs a learned model because it must
recognise arbitrary community builds. A campus is not arbitrary: the ground is
natural blocks plus the surfaces people pave, the buildings are a small,
consistent structural palette, and everything else is planting, water or trim.
A campus is exactly the case where a block-name rule set is not a compromise.

Three rules carry the weight:

1. **Terrain = ground-forming materials, plus extensive flat pavement.** A
   column's terrain height is the topmost ground material. Roads and plazas are
   concrete/quartz/stone, which are also wall materials, so material alone cannot
   separate them -- but *flatness can*. A 5x5 neighbourhood whose topmost block
   varies by at most one level is a surface, not a building; the same material
   with vertical spread is a wall. That single geometric test is what makes the
   split work.

2. **Objects = connected components of the non-terrain blocks.** 26-connectivity
   via scipy.ndimage.label, which is C-speed, so no per-block Python loop.

3. **Detail = the small trim palette** (fences, bars, panes, doors, signs,
   stairs). Kept as its own role because S4 substitutes these for real geometry,
   and because leaving them inside the building blob would make every facade read
   as one solid mass.

Outputs (JSON, plus an ASCII role map for eyeballing):

    out/classify/stats.json          counts per role and funnel
    out/classify/structures.json     one entry per building
    out/classify/trees.json          one entry per tree
    out/classify/terrain.npy         int16 terrain height per column
    out/classify/rolemap.txt         ASCII plan view

    python3 tools/classify.py --bin voxel_data/full/overworld.bin
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "phase2"))
from voxelio import VoxelFile                     # noqa: E402

OUT_DIR = "out/classify"

CAMPUS = (-144, 303, -544, 223)
MAX_Y = 72                    # campus tops out at y=62; leave headroom

#: Role codes stored in the volume.
EMPTY = 0
TERRAIN = 1
STRUCTURE = 2
VEGETATION = 3
WATER = 4
DETAIL = 5

ROLE_NAMES = {EMPTY: "empty", TERRAIN: "terrain", STRUCTURE: "structure",
              VEGETATION: "vegetation", WATER: "water", DETAIL: "detail"}

#: Ground-forming materials. Everything here can BE the ground.
#:
#: `wool` is included deliberately: the sports field in this save is wool at
#: y=5 sitting on grass at y=3, so excluding it would put the field's terrain two
#: blocks low and carve a trench across the middle of the campus.
TERRAIN_MATS = {
    "grass_block", "dirt", "coarse_dirt", "podzol", "mycelium", "farmland",
    "dirt_path", "grass_path", "sand", "red_sand", "gravel", "clay",
    "snow_block", "moss_block", "bedrock", "stone", "deepslate",
    "andesite", "diorite", "granite", "tuff", "calcite",
    # Stone-family SURFACES. These are also wall materials, which is exactly why
    # the flatness test exists rather than a plain material list.
    "cobblestone", "mossy_cobblestone", "smooth_stone", "stone_bricks",
    "cracked_stone_bricks", "mossy_stone_bricks", "chiseled_stone_bricks",
    "bricks", "nether_bricks", "end_stone",
}
TERRAIN_SUFFIX = ("_wool",)          # the field surface

#: Structural palette: walls, floors and roofs of the buildings.
STRUCTURE_MATS = {
    "white_concrete", "light_gray_concrete", "gray_concrete",
    "white_concrete_powder", "light_gray_concrete_powder",
    "quartz_block", "smooth_quartz", "quartz_pillar", "chiseled_quartz_block",
    "smooth_sandstone", "sandstone", "cut_sandstone", "chiseled_sandstone",
    "stone_bricks", "chiseled_stone_bricks", "cracked_stone_bricks",
    "mossy_stone_bricks", "smooth_stone", "polished_andesite",
    "polished_diorite", "polished_granite", "bricks", "nether_bricks",
    "terracotta", "white_terracotta", "light_gray_terracotta",
    "smooth_stone_slab", "prismarine", "prismarine_bricks",
    "iron_block", "gold_block", "glass", "tinted_glass",
    "sea_lantern", "glowstone", "end_stone_bricks",
    "stripped_oak_log", "stripped_birch_log", "stripped_spruce_log",
}
STRUCTURE_SUFFIX = ("_planks", "_concrete", "_terracotta",
                    "_sandstone", "_bricks", "_glass", "_pane")

#: Trim and fittings. S4 replaces these with real geometry, so they are tracked
#: separately rather than being folded into the building blob.
DETAIL_MATS = {
    "iron_bars", "oak_fence", "birch_fence", "spruce_fence", "dark_oak_fence",
    "acacia_fence", "jungle_fence", "cobblestone_wall", "stone_brick_wall",
    "birch_door", "oak_door", "spruce_door", "iron_door", "acacia_door",
    "birch_trapdoor", "oak_trapdoor", "spruce_trapdoor", "iron_trapdoor",
    "birch_stairs", "oak_stairs", "spruce_stairs", "stone_stairs",
    "birch_slab", "oak_slab", "stone_slab", "brick_slab", "quartz_slab",
    "oak_sign", "birch_sign", "birch_wall_sign", "oak_wall_sign",
    "torch", "wall_torch", "lantern", "chain", "rail", "powered_rail",
    "birch_button", "oak_button", "lever", "ladder",
    "chest", "trapped_chest", "barrel", "cauldron", "bed",
    "brown_bed", "red_bed", "white_bed", "light_blue_bed",
    "flower_pot", "armor_stand", "item_frame", "painting",
    "white_carpet", "light_gray_carpet", "red_carpet", "black_carpet",
    "cobweb", "snow", "vine",
    # Fixtures that are plentiful and spread out. Left as structure they acted as
    # bridges between buildings during connected-component labelling.
    "end_rod", "barrier", "scaffolding", "lightning_rod", "redstone_lamp",
    "daylight_detector", "comparator", "repeater", "observer", "piston",
    "sticky_piston", "dispenser", "dropper", "hopper", "note_block",
    "pressure_plate", "oak_pressure_plate", "birch_pressure_plate",
    "stone_pressure_plate", "tripwire_hook", "target", "tnt",
}
DETAIL_SUFFIX = ("_button", "_sign", "_fence", "_gate", "_wall_banner",
                 "_banner", "_carpet", "_stairs", "_slab", "_trapdoor",
                 "_door", "_pane", "_wall", "_rail", "_torch")

#: Planting. Trees are extracted as instanced objects.
VEGETATION_MATS = {
    "grass", "tall_grass", "fern", "large_fern", "dead_bush",
    "dandelion", "poppy", "azure_bluet", "allium", "oxeye_daisy",
    "cornflower", "lilac", "rose_bush", "peony", "sunflower",
    "sugar_cane", "bamboo", "bamboo_sapling", "cactus",
    "brown_mushroom", "red_mushroom", "pumpkin", "melon",
    "wheat", "carrots", "potatoes", "beetroots", "lily_pad",
}
VEGETATION_SUFFIX = ("_leaves", "_log", "_wood", "_sapling")

WATER_MATS = {"water", "flowing_water", "bubble_column", "seagrass", "kelp"}


def role_of(name):
    """Map a Minecraft block name to a role code."""
    n = name.split(":", 1)[-1]
    if n in WATER_MATS:
        return WATER
    if n in VEGETATION_MATS or n.endswith(VEGETATION_SUFFIX):
        return VEGETATION
    if n in DETAIL_MATS or n.endswith(DETAIL_SUFFIX):
        return DETAIL
    if n in STRUCTURE_MATS or n.endswith(STRUCTURE_SUFFIX):
        return STRUCTURE
    if n in TERRAIN_MATS or n.endswith(TERRAIN_SUFFIX):
        return TERRAIN
    # Unmapped blocks are treated as structure: they are far more likely to be
    # part of a build than to be ground, and a mis-roled wall is visible while a
    # mis-roled ground patch silently dents the terrain.
    return STRUCTURE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, dest="bin_path")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--min-structure", type=int, default=40,
                    help="blocks; smaller components are dropped as noise")
    ap.add_argument("--min-tree", type=int, default=12)
    ap.add_argument("--wall-rise", type=int, default=3,
                    help="blocks above terrain that count as wall, for "
                         "separating touching buildings")
    args = ap.parse_args()

    t0 = time.time()
    x0, x1, z0, z1 = CAMPUS
    W, D, H = x1 - x0 + 1, z1 - z0 + 1, MAX_Y

    # ---- load into a role volume -----------------------------------------
    role = np.zeros((W, H, D), dtype=np.uint8)
    name_cache = {}
    unmapped = Counter()

    with VoxelFile(args.bin_path) as vf:
        for ch in vf.iter_chunks():
            cx, cz = ch["chunkX"], ch["chunkZ"]
            if (cx * 16 > x1 or cx * 16 + 15 < x0
                    or cz * 16 > z1 or cz * 16 + 15 < z0):
                continue
            if ch["state"].size == 0:
                continue
            xyz, st = ch["xyz"], ch["state"]
            ix = xyz[:, 0] - x0
            iz = xyz[:, 2] - z0
            iy = xyz[:, 1]
            keep = (ix >= 0) & (ix < W) & (iz >= 0) & (iz < D) & (iy < H)
            if not keep.any():
                continue
            ix, iz, iy, st = ix[keep], iz[keep], iy[keep], st[keep]
            for s in np.unique(st):
                nm = vf.palette[int(s)][0].split(":", 1)[-1]
                r = name_cache.get(nm)
                if r is None:
                    r = role_of(nm)
                    name_cache[nm] = r
                m = st == s
                role[ix[m], iy[m], iz[m]] = r
                if r == STRUCTURE and nm not in STRUCTURE_MATS \
                        and not nm.endswith(STRUCTURE_SUFFIX):
                    unmapped[nm] += int(m.sum())
    print("loaded role volume %s in %.1fs" % (role.shape, time.time() - t0))

    # ---- terrain height, with the flatness test --------------------------
    #
    # top_y / top_role describe the topmost solid block of each column.
    solid = role != EMPTY
    top_y = np.full((W, D), -1, dtype=np.int16)
    top_role = np.zeros((W, D), dtype=np.uint8)
    for y in range(H):
        here = solid[:, y, :]
        top_y = np.where(here, y, top_y)
        top_role = np.where(here, role[:, y, :], top_role)

    # Ground materials are terrain unconditionally.
    is_terrain_surface = (top_role == TERRAIN)
    # Structural materials are terrain only when the local top is flat, which is
    # what separates a paved surface from a wall.
    struct_top = (top_role == STRUCTURE)
    pad = np.full((W + 4, D + 4), -99, dtype=np.int16)
    pad[2:-2, 2:-2] = top_y
    spread = np.zeros((W, D), dtype=np.int16)
    for dy in (-2, -1, 0, 1, 2):
        for dz in (-2, -1, 0, 1, 2):
            nb = pad[2 + dy:2 + dy + W, 2 + dz:2 + dz + D]
            spread = np.maximum(spread, np.abs(nb - top_y))
    flat = struct_top & (spread <= 1)

    # Terrain height: topmost TRUE terrain block, ignoring anything above it.
    terrain_h = np.full((W, D), -1, dtype=np.int16)
    for y in range(H):
        here = (role[:, y, :] == TERRAIN)
        terrain_h = np.where(here, y, terrain_h)
    # Where the surface is flat pavement, that pavement IS the terrain.
    terrain_h = np.where(flat & (top_y > terrain_h), top_y, terrain_h)

    print("terrain columns: %d / %d (%.1f%%)"
          % (int((terrain_h >= 0).sum()), W * D,
             100.0 * (terrain_h >= 0).mean()))

    # ---- objects: connected components -----------------------------------
    from scipy import ndimage

    def components(mask, min_blocks, connectivity=3):
        structure = np.ones((3, 3, 3), dtype=bool) if connectivity == 3 \
            else None
        lab, n = ndimage.label(mask, structure=structure)
        if n == 0:
            return lab, []
        counts = np.bincount(lab.ravel())
        keep = np.nonzero(counts >= min_blocks)[0]
        keep = keep[keep != 0]
        objs = []
        # Slice per label: bbox and dominant material without a Python block loop.
        for lid in keep:
            m = lab == lid
            idx = np.nonzero(m)
            objs.append({
                "id": int(lid),
                "blocks": int(m.sum()),
                "bbox": [[int(idx[0].min()), int(idx[0].max())],
                         [int(idx[1].min()), int(idx[1].max())],
                         [int(idx[2].min()), int(idx[2].max())]],
                "centre_block": [int(x0 + (idx[0].min() + idx[0].max()) // 2),
                                 int((idx[1].min() + idx[1].max()) // 2),
                                 int(z0 + (idx[2].min() + idx[2].max()) // 2)],
                "top_y": int(idx[1].max()),
            })
        return lab, objs

    # ---- separate buildings that touch -----------------------------------
    #
    # Plain labelling of the whole structure mask merged the entire campus into
    # ONE 431,851-block component, because a flat cobblestone path and a string
    # of ground fittings connect every building. Material rules cannot fix that:
    # they are genuinely connected.
    #
    # So label only the parts that RISE above the ground -- walls -- and then
    # grow every remaining block back to its nearest wall voxel. A shared path
    # has no wall of its own, so it attaches to the building it is beside, while
    # two buildings with a gap between them stay separate because the nearest
    # wall voxel is on one side or the other. That is the standard
    # distance-transform watershed trick and it needs no tuning knobs.
    obj_mask = (role == STRUCTURE) | (role == DETAIL)
    # "Rises above the ground" = at least `wall_rise` blocks higher than the
    # terrain in that column. One vectorised comparison, not a Python loop.
    level = np.arange(H, dtype=np.int16)[None, :, None]
    th = np.clip(terrain_h, -1, H - 1)[:, None, :]
    solid_above = level > (th + args.wall_rise)
    core = obj_mask & solid_above
    print("core wall blocks: %d of %d object blocks"
          % (int(core.sum()), int(obj_mask.sum())))

    from scipy import ndimage
    # 6-connectivity: faces only. 26-connectivity joins diagonal-only touches,
    # which is how a single stray block welds two walls together.
    face6 = ndimage.generate_binary_structure(3, 1)
    lab, n = ndimage.label(core, structure=face6)
    print("core wall components: %d" % n)

    # Grow: every object block joins the label of its nearest core voxel.
    if n:
        _, nearest = ndimage.distance_transform_edt(lab == 0, return_indices=True)
        grown = lab[nearest[0], nearest[1], nearest[2]]
        grown = np.where(obj_mask, grown, 0)
    else:
        grown = np.zeros_like(lab)
    lab = grown

    def collect(lab, min_blocks):
        if lab.max() == 0:
            return []
        counts = np.bincount(lab.ravel())
        keep = np.nonzero(counts >= min_blocks)[0]
        keep = keep[keep != 0]
        out = []
        for lid in keep:
            idx = np.nonzero(lab == lid)
            out.append({
                "id": int(lid),
                "blocks": int(counts[lid]),
                "bbox": [[int(idx[0].min()), int(idx[0].max())],
                         [int(idx[1].min()), int(idx[1].max())],
                         [int(idx[2].min()), int(idx[2].max())]],
                "centre_block": [int(x0 + (idx[0].min() + idx[0].max()) // 2),
                                 int((idx[1].min() + idx[1].max()) // 2),
                                 int(z0 + (idx[2].min() + idx[2].max()) // 2)],
                "top_y": int(idx[1].max()),
            })
        return out

    structures = collect(lab, args.min_structure)
    # Largest first: the campus is a handful of big buildings plus outbuildings.
    structures.sort(key=lambda o: -o["blocks"])

    _, trees = components(role == VEGETATION, args.min_tree, connectivity=3)
    trees.sort(key=lambda o: -o["blocks"])

    water_blk = int((role == WATER).sum())

    # ---- report ----------------------------------------------------------
    os.makedirs(args.out, exist_ok=True)
    counts = {ROLE_NAMES[r]: int((role == r).sum())
              for r in (EMPTY, TERRAIN, STRUCTURE, VEGETATION, WATER, DETAIL)}
    stats = {
        "campus": list(CAMPUS),
        "volume": [W, H, D],
        "blocks": int((role != EMPTY).sum()),
        "role_counts": counts,
        "terrain_columns": int((terrain_h >= 0).sum()),
        "structure_objects": len(structures),
        "tree_objects": len(trees),
        "water_blocks": water_blk,
        "unmapped_structural_blocks": dict(unmapped.most_common(20)),
        "largest_structures": [
            {"blocks": o["blocks"], "centre": o["centre_block"],
             "top_y": o["top_y"]} for o in structures[:10]],
        "largest_trees": [
            {"blocks": o["blocks"], "centre": o["centre_block"]}
            for o in trees[:8]],
        "elapsed_s": round(time.time() - t0, 1),
    }
    with open(os.path.join(args.out, "stats.json"), "w") as fh:
        json.dump(stats, fh, indent=2, ensure_ascii=False)
    np.save(os.path.join(args.out, "structure_labels.npy"), lab)
    with open(os.path.join(args.out, "structures.json"), "w") as fh:
        json.dump(structures, fh, indent=2)
    with open(os.path.join(args.out, "trees.json"), "w") as fh:
        json.dump(trees, fh, indent=2)
    np.save(os.path.join(args.out, "terrain.npy"), terrain_h)
    np.save(os.path.join(args.out, "rolevolume.npy"), role)

    # ASCII plan view, top surface role per 8-block cell. This is the quick
    # "does the split look right" check -- the field should come out as terrain,
    # the buildings as structure, the planting as vegetation.
    glyph = {EMPTY: ".", TERRAIN: "-", STRUCTURE: "#", VEGETATION: "T",
             WATER: "~", DETAIL: ":"}
    step = 8
    lines = []
    for i in range(0, W, step):
        row = []
        for j in range(0, D, step):
            cell = top_role[i:i + step, j:j + step]
            if cell.size == 0 or (cell == EMPTY).all():
                row.append(".")
            else:
                u, c = np.unique(cell[cell != EMPTY], return_counts=True)
                row.append(glyph.get(int(u[c.argmax()]), "?"))
        lines.append("%5d %s" % (x0 + i, "".join(row)))
    with open(os.path.join(args.out, "rolemap.txt"), "w") as fh:
        fh.write("top-surface role, %d-block cells, x rows z cols\n" % step)
        fh.write("  - terrain   # structure   T vegetation   ~ water   : detail\n")
        fh.write("\n".join(lines) + "\n")

    print(json.dumps({k: v for k, v in stats.items()
                      if k not in ("largest_structures", "largest_trees",
                                   "unmapped_structural_blocks")},
                     indent=2, ensure_ascii=False))
    print("report: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
