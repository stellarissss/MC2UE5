# -*- coding: utf-8 -*-
"""
block_families.py -- block name -> material family, single source of truth.

The rendering rework maps Minecraft block names onto a small set of CC0 material
families (the 17 imported from Poly Haven). Every place that needs the mapping
-- the census, the geometry assembler, the material assigner -- imports this
module so the mapping cannot drift between them.

Rules are ordered substring matches against the de-namespaced name, longest
behaviour first. Blocks that match no rule land in ``"other"`` and are surfaced
by ``unmapped_names()`` so they can be triaged rather than silently mis-shaded.

This is the *only* file that decides the mapping. Changing a family here changes
it everywhere.
"""

#: Material family -> the CC0 asset key it came from (for documentation /
#: so the assembler can verify a MaterialInstance exists for every family).
#: Keys must match the manifest in assets_cc0/polyhaven/manifest.json.
FAMILIES = (
    "grass", "path", "soil", "asphalt", "concrete", "plaster", "brick",
    "granite", "tiles", "roof", "wood", "bark", "leaves", "metal", "gravel",
    "rock", "fabric", "quartz", "greystone",
)

#: (substring, family). First match wins; the list is ordered so the more
#: specific tokens are tested before the catch-alls ("stone" is near the end).
_RULES = (
    # glass / translucent (no CC0 texture yet -> treated as glass family)
    ("glass", "fabric"),          # placeholder until a glass material lands
    ("ice", "fabric"),
    # vegetation
    ("leaves", "leaves"),
    ("sapling", "leaves"),
    ("flower", "leaves"),
    ("fern", "leaves"),
    ("vine", "leaves"),
    ("lily", "leaves"),
    ("grass", "grass"),           # grass_block, grass_path, tall grass
    ("tall_grass", "grass"),
    ("_bush", "leaves"),
    ("cactus", "leaves"),
    ("sugar_cane", "leaves"),
    ("bamboo", "leaves"),
    ("wheat", "leaves"),
    ("carrot", "leaves"),
    ("potato", "leaves"),
    ("beetroot", "leaves"),
    ("melon", "leaves"),
    ("pumpkin", "leaves"),
    ("cocoa", "leaves"),
    ("sweet_berry", "leaves"),
    # ground
    ("grass_path", "path"),
    ("dirt_path", "path"),
    ("dirt", "soil"),
    ("podzol", "soil"),
    ("mycelium", "soil"),
    ("farmland", "soil"),
    ("coarse_dirt", "soil"),
    ("rooted_dirt", "soil"),
    ("mud", "soil"),
    ("gravel", "gravel"),
    ("sand", "gravel"),            # sand -> gravel family (sandstone tones)
    ("sandstone", "gravel"),
    ("clay", "soil"),
    # rock / masonry
    #
    # Split by *colour*, because one grey family is not enough and the mapping
    # is what the eye checks first. Measured on the campus surface within 60
    # blocks of the spawn: quartz_block alone is 31% of it, and Minecraft
    # renders that **near-white** -- sending it to the brown `rock` texture made
    # the whole courtyard read as bare soil, which is the "everything is one
    # grey/brown mass" report.
    ("quartz", "quartz"),                  # white smooth stone (MC: near-white)
    ("cobblestone", "greystone"),          # MC: mid grey
    ("cobble", "greystone"),
    ("stone_bricks", "greystone"),
    ("smooth_stone", "greystone"),
    ("_bricks", "brick"),
    ("brick", "brick"),
    ("terracotta", "brick"),
    ("granite", "granite"),
    ("diorite", "granite"),
    ("andesite", "granite"),
    ("basalt", "rock"),
    ("blackstone", "rock"),
    ("deepslate", "rock"),
    ("obsidian", "rock"),
    ("bedrock", "rock"),
    ("prismarine", "rock"),
    ("_ore", "rock"),
    ("netherrack", "rock"),
    ("end_stone", "rock"),
    ("purpur", "rock"),
    ("stone", "greystone"),
    ("concrete_powder", "gravel"),
    ("concrete", "concrete"),
    ("plaster", "plaster"),
    ("_wall", "plaster"),          # stone walls / brick walls -> plaster-toned
    # wood
    ("_log", "bark"),
    ("_stem", "bark"),
    ("_wood", "bark"),
    ("_hyphae", "bark"),
    ("_planks", "wood"),
    ("_door", "wood"),
    ("_trapdoor", "wood"),
    ("_fence", "wood"),
    ("_fence_gate", "wood"),
    ("_sign", "wood"),
    ("_button", "wood"),
    ("_pressure_plate", "wood"),
    ("_stairs", "wood"),
    ("_slab", "wood"),
    ("bookshelf", "wood"),
    ("chest", "wood"),
    ("crafting_table", "wood"),
    ("ladder", "wood"),
    ("composter", "wood"),
    ("barrel", "wood"),
    ("loom", "wood"),
    ("cartography_table", "wood"),
    ("fletching_table", "wood"),
    ("smithing_table", "wood"),
    ("note_block", "wood"),
    ("jukebox", "wood"),
    # roofs / tiles
    ("roof", "roof"),
    ("slate", "roof"),
    ("_tile", "tiles"),
    # metal
    ("iron", "metal"),
    ("gold", "metal"),
    ("copper", "metal"),
    ("_rail", "metal"),
    ("chain", "metal"),
    ("hopper", "metal"),
    ("cauldron", "metal"),
    ("anvil", "metal"),
    ("bell", "metal"),
    ("lantern", "metal"),
    ("bar", "metal"),
    ("chain_command", "metal"),
    ("beacon", "metal"),
    ("conduit", "metal"),
    # fabric / soft
    ("wool", "fabric"),
    ("carpet", "fabric"),
    ("_bed", "fabric"),
    ("banner", "fabric"),
    ("canvas", "fabric"),
    # misc that should read as something neutral
    ("shulker", "plaster"),
    ("target", "plaster"),
    ("lodestone", "rock"),
    ("spawner", "rock"),
    ("tnt", "fabric"),
    ("honey", "brick"),
    ("mushroom_block", "bark"),
    ("wart", "leaves"),
    ("kelp", "leaves"),
    ("seagrass", "leaves"),
    ("coral", "rock"),
    ("scaffolding", "wood"),
    ("torch", "metal"),
    ("candle", "metal"),
    ("lectern", "wood"),
    ("piston", "metal"),
    ("observer", "rock"),
    ("dispenser", "rock"),
    ("dropper", "rock"),
    ("furnace", "rock"),
    ("blast_furnace", "rock"),
    ("smoker", "rock"),
    ("campfire", "wood"),
    ("soul_campfire", "wood"),
    ("glowstone", "fabric"),
    ("sea_lantern", "fabric"),
    ("lantern", "metal"),
    ("redstone_lamp", "fabric"),
    ("daylight_detector", "wood"),
    ("repeater", "rock"),
    ("comparator", "rock"),
    ("lever", "metal"),
    ("redstone", "fabric"),
    ("redstone_torch", "fabric"),
    ("tripwire", "fabric"),
    ("snow", "fabric"),
    ("powder_snow", "fabric"),
    ("cobweb", "fabric"),
    ("web", "fabric"),
    # flowers & small plants -> leaves (they read as planting)
    ("tulip", "leaves"),
    ("daisy", "leaves"),
    ("poppy", "leaves"),
    ("bluet", "leaves"),
    ("allium", "leaves"),
    ("cornflower", "leaves"),
    ("_mushroom", "leaves"),
    ("fungus", "leaves"),
    ("sprouts", "leaves"),
    ("roots", "leaves"),
    ("chorus_plant", "leaves"),
    ("potted_", "plaster"),        # the pot, not the plant
    # ore / gem / mineral blocks (rare in a campus, but shade them sensibly)
    ("coal_block", "rock"),
    ("lapis_block", "rock"),
    ("diamond_block", "metal"),
    ("emerald_block", "metal"),
    ("netherite_block", "metal"),
    ("ancient_debris", "rock"),
    ("magma_block", "rock"),
    ("bone_block", "plaster"),
    ("soul_soil", "soil"),
    ("nylium", "soil"),
    ("bee_nest", "bark"),
    ("beehive", "bark"),
    ("enchanting_table", "wood"),
    ("brewing_stand", "metal"),
    ("end_rod", "metal"),
    ("end_portal_frame", "rock"),
    ("shroomlight", "fabric"),
)


def family(block_name):
    """Map a namespaced block name to a material family, or 'other'."""
    n = block_name.split(":", 1)[-1]
    for token, fam in _RULES:
        if token in n:
            return fam
    return "other"


def unmapped_names(names):
    """-> list of names that map to 'other'."""
    return sorted(n for n in names if family(n) == "other")


def family_counts(names, counts=None):
    """-> dict family -> int, over a list of names (optionally weighted)."""
    out = {}
    for i, n in enumerate(names):
        w = counts[i] if counts else 1
        f = family(n)
        out[f] = out.get(f, 0) + w
    return out


# --------------------------------------------------------------------------- #
# colour variants (S5.5)
# --------------------------------------------------------------------------- #
#
# `family()` above answers "what *material type* is this", and deliberately
# ignores MC's colour prefix: `lime_wool` and `green_wool` both -> "fabric".
# That is correct for `family()` and MUST stay that way -- three callers depend
# on its return value being a key of FAMILIES/ATLAS_FAMILIES
# (extract_structures.build_material_volume indexes the atlas with it,
# reference_map.py and campus_map.py look families up by it). Making `family()`
# return "fabric_lime" would silently shift or break all three.
#
# The colour variants live in a *separate* function, `family_key()`, which
# returns "fabric_lime" when a colour variant applies and otherwise falls back
# to exactly what `family()` returns. `variant()` exposes the parsed
# (base_family, colour) for the baker. Nothing here changes `family()`.
#
# Spec: docs/colour_variants.md (ART-S5b), sections 1.1 and 4.2.

#: MC's 16 dye colours. Sorted longest-first so "light_blue" / "light_gray" are
#: tested before "blue" / "gray" when matched as a prefix.
DYE_COLOURS = (
    "light_blue", "light_gray", "white", "orange", "magenta", "yellow", "lime",
    "pink", "gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
)

#: The block base token (colour prefix removed) that makes something a variant.
#: `banner` is absent deliberately: `white_wall_banner` must fall through to
#: `plaster` (the `_wall` rule), and a plain `white_banner` (a wall banner) is
#: not a surface we bake for. `stained_glass` is here even though our material
#: is opaque -- colour is still better than near-white (see spec 1.3).
VARIANT_TOKENS = (
    "wool", "carpet", "bed", "stained_glass_pane", "stained_glass",
    "concrete", "terracotta",
)

#: Suffixes a variant token can carry (trim variants of the same block).
VARIANT_TRIM_SUFFIXES = ("_wall", "_slab", "_stairs")

#: If any of these appear in the de-namespaced name it is NOT a colour variant,
#: no matter how it parses. Each is a real false positive from the spec.
VARIANT_EXCLUDE = (
    "nether_brick",        # red_nether_brick_* -> brick, NOT brick_red
    "_concrete_powder",    # powder is a different block -> gravel
    "glazed",              # glazed terracotta has a pattern -> brick, as-is
    "blue_ice",            # no ice family; goes to fabric, NOT fabric_blue
)

#: variant base token -> the FAMILIES family it belongs to.
VARIANT_BASE_FAMILY = {
    "wool": "fabric", "carpet": "fabric", "bed": "fabric",
    "stained_glass_pane": "fabric", "stained_glass": "fabric",
    "concrete": "concrete", "terracotta": "brick",
}

#: variant name -> target MC in-world appearance colour (RGB 0-255).
#:
#: The single source of truth shared by `bake_colour_variants.py` (it bakes
#: `target / base_mean` per channel) and by `family_key()` (a variant only
#: exists if we actually baked a texture for it). Keeping one table is the same
#: reason `rolemap` and `block_families` were merged: two tables drift.
#:
#: Values are the MC *in-world* colours, not the mapItem base colours -- the
#: spec (1.4) measured the difference as large (white_concrete is (207,213,214)
#: in world but #FFFFFF on the map) and chose in-world because the target is
#: "what the player sees".
VARIANT_TARGET = {
    # ---- concrete (28 blocks in the palette) -----------------------------
    "concrete_white":      (207, 213, 214),
    "concrete_light_gray": (125, 125, 115),
    "concrete_green":      ( 73,  91,  36),
    "concrete_cyan":       ( 21, 119, 136),
    "concrete_light_blue": ( 36, 137, 199),
    "concrete_red":        (142,  33,  33),
    "concrete_lime":       ( 94, 169,  25),
    "concrete_blue":       ( 45,  47, 143),
    # ---- brick / terracotta (blurred base) -------------------------------
    "brick_pink":          (160,  77,  78),
    "brick_cyan":          ( 87,  92,  92),
    "brick_green":         ( 76,  82,  42),
    # ---- fabric / wool ---------------------------------------------------
    "fabric_lime":         (112, 185,  25),
    "fabric_green":        ( 84, 109,  27),
    "fabric_white":        (233, 236, 236),
    "fabric_light_blue":   ( 58, 175, 217),
    "fabric_red":          (161,  39,  34),
    "fabric_yellow":       (248, 198,  39),
    "fabric_orange":       (240, 118,  19),
    "fabric_black":        ( 20,  21,  25),
    "fabric_brown":        (114,  71,  40),
    "fabric_blue":         ( 53,  57, 157),
}


def variant(block_name):
    """-> (variant_name, base_family) if this block has a colour variant we
    baked, else (None, None).

    Rules (spec 4.2), applied in this order -- order is the whole contract,
    because `("wool", "fabric")` in `_RULES` would otherwise swallow
    `lime_wool` first:

      1. the name starts with `<dye>_`; strip it to get ``bare``;
      2. drop a trim suffix from ``bare`` (``white_wool_slab`` -> ``wool``);
      3. ``bare`` must be a VARIANT_TOKENS entry;
      4. any VARIANT_EXCLUDE substring anywhere in the name bails out;
      5. a baked texture must exist (`VARIANT_TARGET`).

    Anything failing 3-5 falls back to `family()`, i.e. the base material.
    """
    n = block_name.split(":", 1)[-1]
    for bad in VARIANT_EXCLUDE:
        if bad in n:
            return None, None
    colour = None
    for dye in DYE_COLOURS:
        if n.startswith(dye + "_"):
            colour = dye
            bare = n[len(dye) + 1:]
            break
    if colour is None:
        return None, None
    for suf in VARIANT_TRIM_SUFFIXES:
        if bare.endswith(suf):
            bare = bare[:-len(suf)]
            break
    if bare not in VARIANT_TOKENS:
        return None, None
    name = "%s_%s" % (VARIANT_BASE_FAMILY[bare], colour)
    if name not in VARIANT_TARGET:
        return None, None          # parsed as a variant but we did not bake it
    return name, VARIANT_BASE_FAMILY[bare]


def family_key(block_name):
    """-> ``"fabric_lime"`` when a colour variant applies, else ``family(name)``.

    This is the function for anything that needs to *distinguish* colours. It
    never changes the return type of `family()` and never returns a name that
    has no baked texture.
    """
    v, _base = variant(block_name)
    return v if v else family(block_name)

