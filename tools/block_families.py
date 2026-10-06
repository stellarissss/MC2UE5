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
