"""
Semantic classification for MC2WV2 voxels.

This is the *pluggable* half of the mc3d pipeline. The paper's 3D U-Net is a
per-voxel classifier trained on Minecraft's canonical assets; we do not have
those weights (the authors' repo is still "Coming Soon" as of 2025-12) and this
sandbox has no GPU, so the default provider is a deterministic classifier built
from block identity + voxel neighbourhood.

The important property is the *interface*, not the accuracy: every provider
returns the same labels, so swapping in the U-Net later changes nothing
downstream.

    python3 semantic.py --provider cpu   --dim overworld
    python3 semantic.py --provider unet  --dim overworld --weights unet.pt --device cuda

Label set (ids are stable; do not renumber -- files already reference them):

    0  UNKNOWN       no data
    1  AIR           air / cave_air / void_air (never emitted; voxels are sparse)
    2  TERRAIN       can form a continuous ground surface
    3  SUBSURFACE    stone/dirt below the surface: terrain volume, not surface
    4  WATER         water + waterlogged variants
    5  LAVA          emissive liquid, rendered separately
    6  VEGETATION    trees, leaves, flowers, grass, saplings, crops
    7  STRUCTURE     built blocks: walls, bricks, concrete, beams, slabs, ...
    8  PROP          small decor that should stay as voxels (torches, fences)
    9  GLASS         transparent architecture panes
    10 BEDROCK       the indestructible world floor; never a visible surface
"""

import json
import os
import re

import numpy as np

LABEL_UNKNOWN = 0
LABEL_AIR = 1
LABEL_TERRAIN = 2
LABEL_SUBSURFACE = 3
LABEL_WATER = 4
LABEL_LAVA = 5
LABEL_VEGETATION = 6
LABEL_STRUCTURE = 7
LABEL_PROP = 8
LABEL_GLASS = 9
LABEL_BEDROCK = 10

LABEL_NAMES = {
    LABEL_UNKNOWN: "UNKNOWN",
    LABEL_AIR: "AIR",
    LABEL_TERRAIN: "TERRAIN",
    LABEL_SUBSURFACE: "SUBSURFACE",
    LABEL_WATER: "WATER",
    LABEL_LAVA: "LAVA",
    LABEL_VEGETATION: "VEGETATION",
    LABEL_STRUCTURE: "STRUCTURE",
    LABEL_PROP: "PROP",
    LABEL_GLASS: "GLASS",
    LABEL_BEDROCK: "BEDROCK",
}

# Only these feed the heightmap (paper step 1: "non-terrain voxels are hidden").
TERRAIN_LIKE = frozenset((LABEL_TERRAIN, LABEL_SUBSURFACE))

# Colours for debug visualisations (RGB 0-255).
LABEL_COLORS = {
    LABEL_UNKNOWN: (60, 60, 60),
    LABEL_AIR: (0, 0, 0),
    LABEL_TERRAIN: (120, 200, 90),
    LABEL_SUBSURFACE: (110, 90, 70),
    LABEL_WATER: (50, 110, 220),
    LABEL_LAVA: (230, 120, 30),
    LABEL_VEGETATION: (40, 170, 60),
    LABEL_STRUCTURE: (190, 185, 175),
    LABEL_PROP: (230, 200, 60),
    LABEL_GLASS: (170, 220, 235),
    LABEL_BEDROCK: (40, 40, 45),
}

AIR_NAMES = frozenset(("minecraft:air", "minecraft:cave_air", "minecraft:void_air"))

# --------------------------------------------------------------------------- #
# block-name -> label rules
#
# Structure matters, and exact names come first on purpose. Substring matching
# is the fallback, not the primary mechanism: "bed" is a substring of
# "bedrock" and "grass" of "grass_block", so a substring-first classifier
# files the world's floor under PROP and its grass under VEGETATION. Both of
# those silently corrupt the terrain heightmap, which is the one thing in this
# pipeline that must not be wrong.
#
# Minecraft block names are compositional ("oak_slab",
# "polished_blackstone_brick_wall"), so the fallback still generalises to modded
# and variant names it has never seen.
# --------------------------------------------------------------------------- #

# Exact names, checked before anything else. Keep this list authoritative for
# the blocks this project actually uses (276 distinct types in the overworld).
_EXACT = {
    # world floor
    "bedrock": LABEL_BEDROCK,
    # liquids
    "water": LABEL_WATER, "lava": LABEL_LAVA,
    # ground surfaces
    "grass_block": LABEL_TERRAIN, "sand": LABEL_TERRAIN, "gravel": LABEL_TERRAIN,
    "snow": LABEL_TERRAIN, "snow_block": LABEL_TERRAIN, "clay": LABEL_TERRAIN,
    "podzol": LABEL_TERRAIN, "mycelium": LABEL_TERRAIN,
    "terracotta": LABEL_TERRAIN, "soul_sand": LABEL_TERRAIN,
    "soul_soil": LABEL_TERRAIN, "magma_block": LABEL_TERRAIN,
    "end_stone": LABEL_TERRAIN, "netherrack": LABEL_TERRAIN,
    "obsidian": LABEL_TERRAIN,
    # subsoil
    "dirt": LABEL_SUBSURFACE, "coarse_dirt": LABEL_SUBSURFACE,
    "rooted_dirt": LABEL_SUBSURFACE,
    # a bare "grass"/"fern"/"dead_bush" is a plant, but grass_BLOCK is ground --
    # both are handled: the block form is matched above, these are the plants
    "grass": LABEL_VEGETATION, "fern": LABEL_VEGETATION,
    "dead_bush": LABEL_VEGETATION, "seagrass": LABEL_VEGETATION,
    "tall_seagrass": LABEL_VEGETATION,
}

_WATER_NAMES = frozenset((
    "water", "bubble_column", "tipped_water", "flowing_water",
))

_LAVA_NAMES = frozenset(("lava", "flowing_lava",))

_VEG_SUBSTRINGS = (
    "leaves", "_log", "sapling", "_tree", "vine", "flower",
    "mushroom", "roots", "sprouts", "stems", "fungus", "azalea", "moss",
    "lily", "kelp", "wheat", "carrots", "potatoes", "beetroot",
    "melon", "pumpkin", "sugar_cane", "bamboo", "chorus",
    "lily_pad", "sculk", "twisting", "weeping",
    "crimson", "warped", "nylium", "shroomlight", "candle",
    "chorus_plant", "chorus_flower", "nether_sprouts", "crimson_roots",
    "warped_roots", "crimson_stem", "warped_stem", "twisting_vines",
    "weeping_vines", "crimson_fungus", "warped_fungus", "glow_lichen",
)

_STRUCT_SUBSTRINGS = (
    "concrete", "terracotta", "brick", "stone_brick", "cobblestone", "masonry",
    "wall", "_slab", "stairs", "fence", "gate", "beam", "pillar", "planks",
    "hyphen", "diorite", "andesite", "granite", "_tile", "_tiles",
    "quartz", "sandstone", "prismarine", "purpur", "end_stone_brick",
    "basalt", "blackstone", "polished", "cut_", "chiseled", "smooth_",
    "bricks", "mud", "packed", "suspicious", "_wood",
)

_GLASS_SUBSTRINGS = ("glass_pane", "stained_glass", "_glass")

# Small decor. Matched on whole words, not raw substrings -- see the note above
# about "bed" in "bedrock".
_PROP_EXACT = frozenset((
    "torch", "wall_torch", "soul_torch", "redstone_torch", "lantern",
    "soul_lantern", "lever", "stone_button", "polished_blackstone_button",
    "tripwire", "tripwire_hook", "pressure_plate", "light_weighted_pressure_plate",
    "heavy_weighted_pressure_plate", "stone_pressure_plate", "polished_blackstone_pressure_plate",
    "rail", "powered_rail", "detector_rail", "activator_rail",
    "ladder", "scaffolding", "carpet", "flower_pot", "skull", "skeleton_skull",
    "skeleton_wall_skull", "wither_skeleton_skull", "creeper_head",
    "barrel", "chest", "trapped_chest", "furnace", "blast_furnace", "smoker",
    "hopper", "brewing_stand", "beacon", "bell", "turtle_egg", "brush",
    "grindstone", "smithing_table", "loom", "lectern", "stonecutter",
    "composter", "target", "amethyst_cluster", "cactus", "scaffolding",
    "wall_sign", "standing_sign", "wall_banner", "standing_banner",
))

# Fences and gates are thin decorative boundaries, not solid architecture: they
# must never bulk up the terrain, and as props they stay as instanced detail.
_PROP_SUBSTRINGS = ("_candle", "candle_cake", "cave_vines", "sculk_sensor",
                    "sculk", "chain", "_rail", "_fence", "_fence_gate",
                    "_trapdoor", "_door", "_button")

# Tokens that must not be treated as vegetation even though a generic token
# matches: "oak_wood" is building material, "oak_log" is not.
_VEG_EXCLUDE = ("_wood", "wood")


def _strip_ns(name):
    return name.split(":", 1)[1] if ":" in name else name


def classify_name(block_name):
    """Pure function: block name -> label id. No voxel context needed."""
    if block_name in AIR_NAMES:
        return LABEL_AIR
    n = _strip_ns(block_name)

    # 1. exact names -- authoritative, and immune to substring accidents
    hit = _EXACT.get(n)
    if hit is not None:
        return hit

    # 2. liquids
    if n in _LAVA_NAMES:
        return LABEL_LAVA
    if n in _WATER_NAMES or n.endswith("_water"):
        return LABEL_WATER

    # 3. exact prop names, then word-ish prop substrings
    if n in _PROP_EXACT:
        return LABEL_PROP
    for s in _PROP_SUBSTRINGS:
        if s in n:
            return LABEL_PROP

    # 4. glass before structure: glass is transparent architecture
    for s in _GLASS_SUBSTRINGS:
        if s in n:
            return LABEL_GLASS

    # 5. vegetation
    for s in _VEG_SUBSTRINGS:
        if s in n:
            if any(x in n for x in _VEG_EXCLUDE):
                break
            return LABEL_VEGETATION

    # 6. built structure
    for s in _STRUCT_SUBSTRINGS:
        if s in n:
            return LABEL_STRUCTURE

    # Default: treat as terrain so it participates in the heightmap rather than
    # punching holes. This mirrors the paper's "unrecognised blocks fall back to
    # terrain" behaviour for modded content (this save reports was_modded=true).
    return LABEL_TERRAIN


def build_name_label_map(palette):
    """palette: [(name, props), ...] -> int32 array of label ids."""
    return np.array([classify_name(nm) for (nm, _pk) in palette], dtype=np.int32)


def load_name_label_map(voxel_path):
    """Convenience: build the remap array directly for a .bin's palette."""
    import voxelio as vio
    with vio.VoxelFile(voxel_path) as vf:
        return build_name_label_map(vf.palette), vf.palette


def parse_properties(props):
    """'type=bottom;waterlogged=false' -> {'type':'bottom','waterlogged':'false'}"""
    if not props:
        return {}
    out = {}
    for kv in props.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #

class SemanticProvider(object):
    """
    Interface. A provider maps a chunk's voxels to a label array.

    Implementations must accept and return the same shapes so they are
    interchangeable:

        labels = provider.label_chunk(palette, state, y, origin_y)
          palette : [(name, props), ...]        the chunk's global palette
          state   : int32[N]  global palette index per voxel
          y       : int32[N]  world Y per voxel
          origin_y: int  chunk base Y (0 for overworld)
          returns : uint8[N]  label id per voxel, same order as `state`
    """

    name = "abstract"

    def label_chunk(self, palette, state, y, origin_y=0):
        raise NotImplementedError

    def describe(self):
        return {"provider": self.name}


class RuleSemanticProvider(SemanticProvider):
    """
    Deterministic CPU classifier: block identity + a waterlogged pass.

    This is the default. It needs no training data and no GPU, and it handles
    the save's actual content well because the map is largely built from
    standard blocks (276 distinct types in the overworld).
    """

    name = "cpu"

    def __init__(self, palette):
        self.palette = palette
        self.base = build_name_label_map(palette)
        # Palette indices whose *state* is waterlogged. Waterlogged blocks are
        # still solid (fences, slabs, stairs) but must not become terrain, and
        # water occupying their cell must still be found for the water plane.
        self.waterlogged = np.array(
            [1 if parse_properties(pk).get("waterlogged") == "true" else 0
             for (_nm, pk) in palette], dtype=bool)

    def label_chunk(self, palette, state, y, origin_y=0):
        lab = self.base[state]
        # waterlogged solid -> PROP (kept as voxel geometry, excluded from the
        # terrain heightmap so it does not drag the surface down)
        lab = np.where(self.waterlogged[state] & (lab != LABEL_WATER),
                       LABEL_PROP, lab)
        return lab.astype(np.uint8)

    def describe(self):
        counts = Counter_of(self.base)
        return {
            "provider": self.name,
            "palette_entries": len(self.palette),
            "label_histogram": {LABEL_NAMES.get(int(k), int(k)): int(v)
                                for k, v in counts.items()},
        }


def Counter_of(arr):
    vals, cnts = np.unique(arr, return_counts=True)
    return dict(zip(vals.tolist(), cnts.tolist()))


class UnetSemanticProvider(SemanticProvider):
    """
    Placeholder for the paper's 3D U-Net.

    Kept as a real class (not a TODO comment) so that the plumbing -- argument
    parsing, output writing, downstream consumption -- is exercised and tested
    now. Only the inference call is missing, and it fails loudly rather than
    silently degrading, so nobody ships rule-based output believing it is
    network output.
    """

    name = "unet"

    def __init__(self, weights=None, device="cpu", block_size=(256, 128, 256),
                 tile_overlap=20):
        self.weights = weights
        self.device = device
        self.block_size = block_size
        self.tile_overlap = tile_overlap

    def label_chunk(self, palette, state, y, origin_y=0):
        raise RuntimeError(
            "UnetSemanticProvider.label_chunk is not implemented: the mc3d "
            "authors have not released their trained weights "
            "(github.com/seanhlewis/mc3d is still 'Coming Soon').\n"
            "Use --provider cpu for the deterministic pipeline, or supply "
            "weights via --weights and implement the forward pass here.\n"
            "The paper's 3D U-Net reaches 97.8% mIoU (isolated) / 88.6% "
            "(overlapping structures) and takes 84% of total runtime, so this "
            "is the one stage that genuinely needs a GPU.")

    def describe(self):
        return {"provider": self.name, "weights": self.weights,
                "device": self.device, "implemented": False}


def make_provider(kind, voxel_path=None, **kw):
    if kind == "cpu":
        import voxelio as vio
        if voxel_path is None:
            raise ValueError("cpu provider needs voxel_path")
        with vio.VoxelFile(voxel_path) as vf:
            return RuleSemanticProvider(vf.palette)
    if kind == "unet":
        return UnetSemanticProvider(
            weights=kw.get("weights"), device=kw.get("device", "cpu"),
            block_size=kw.get("block_size", (256, 128, 256)),
            tile_overlap=kw.get("tile_overlap", 20))
    raise ValueError("unknown provider %r (cpu|unet)" % kind)
