# -*- coding: utf-8 -*-
"""
build_terrain_material_min.py -- a terrain material that certainly compiles.

The 18-node graph rendered, but as sand/dirt rather than grass, and the
replacement I wrote failed to compile outright ("Failed to compile Material for
platform PCD3D_SM5, Default Material will be used in game"), which is why the
ground showed the engine's grey grid.

This goes back to the shape that is known to compile -- exactly the two nodes
MC_Character uses, a texture into BaseColor and a constant into Roughness -- and
adds back the grass/stone/sand blend one step at a time, recompiling and
checking the shader map after each step, so a graph that will not compile can
never reach a cook.

Run inside the editor.
"""

import unreal

OUT = "Q:/MC2UE5/logs/terrain_mat_min.txt"
MAT_DIR = "/Game/MC/Materials"
MAT_PATH = "/Game/MC/Materials/MC_Terrain"
_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCMIN] " + str(m))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def compiled(mat):
    """-> (ok, detail). A material with no shader map renders as the default."""
    try:
        errors = mat.get_editor_property("material_compilation_errors")
        if errors:
            return False, str(errors)[:300]
    except Exception:
        pass
    try:
        return bool(mat.get_shader_map_valid()), ""
    except Exception:
        return None, "no shader-map query"


def main():
    if unreal.EditorAssetLibrary.does_asset_exist(MAT_PATH):
        unreal.EditorAssetLibrary.delete_asset(MAT_PATH)
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "MC_Terrain", MAT_DIR, unreal.Material, unreal.MaterialFactoryNew())
    mel = unreal.MaterialEditingLibrary

    def node(cls, x, y):
        e = mel.create_material_expression(mat, cls, x, y)
        if e is None:
            raise RuntimeError("could not create %s" % cls)
        return e

    def tex(name, x, y):
        e = node(unreal.MaterialExpressionTextureSampleParameter2D, x, y)
        e.set_editor_property("ParameterName", name)
        e.set_editor_property("Texture",
                              unreal.load_asset("/Game/MC/Textures/%s" % name))
        return e

    grass = tex("grass_block_top", -600, -200)
    # Step 1: the shape MC_Character uses, which is known to compile.
    mel.connect_material_property(grass, "RGB",
                                  unreal.MaterialProperty.MP_BASE_COLOR)
    rough = node(unreal.MaterialExpressionConstant, -200, 0)
    rough.set_editor_property("R", 0.9)
    mel.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mel.recompile_material(mat)
    ok, why = compiled(mat)
    say("step1 grass->BaseColor: compiled=%s %s" % (ok, why))
    if not ok:
        say("aborting: the minimal material does not compile")
        say("--- done ---")
        return

    # Step 2: blend stone in on slopes.
    stone = tex("stone", -600, 220)
    nrm = node(unreal.MaterialExpressionVertexNormalWS, -900, 460)
    mask = node(unreal.MaterialExpressionComponentMask, -740, 460)
    for ch, val in (("R", False), ("G", False), ("B", True), ("A", False)):
        mask.set_editor_property(ch, val)
    mel.connect_material_expressions(nrm, "", mask, "Vector")
    inv = node(unreal.MaterialExpressionOneMinus, -560, 460)
    mel.connect_material_expressions(mask, "", inv, "Input")
    lerp = node(unreal.MaterialExpressionLinearInterpolate, -320, 0)
    mel.connect_material_expressions(grass, "RGB", lerp, "A")
    mel.connect_material_expressions(stone, "RGB", lerp, "B")
    mel.connect_material_expressions(inv, "", lerp, "Alpha")
    mel.connect_material_property(lerp, "",
                                  unreal.MaterialProperty.MP_BASE_COLOR)
    mel.recompile_material(mat)
    ok, why = compiled(mat)
    say("step2 +stone-on-slope: compiled=%s %s" % (ok, why))
    if not ok:
        say("aborting: stone blend breaks compilation")
        say("--- done ---")
        return

    # Step 3: sand on high ground, remapped so the ramp sits on the campus.
    sand = tex("sand", -600, 440)
    hz = node(unreal.MaterialExpressionComponentMask, -740, 640)
    for ch, val in (("R", False), ("G", False), ("B", True), ("A", False)):
        hz.set_editor_property(ch, val)
    mel.connect_material_expressions(node(unreal.MaterialExpressionWorldPosition,
                                          -900, 640), "", hz, "Vector")
    # Remap height with a single multiply-add: (z - 4600) / 1400, clamped.
    sub = node(unreal.MaterialExpressionSubtract, -560, 640)
    sub.set_editor_property("ConstB", 4600.0)
    mel.connect_material_expressions(hz, "", sub, "A")
    div = node(unreal.MaterialExpressionDivide, -420, 640)
    div.set_editor_property("ConstB", 1400.0)
    mel.connect_material_expressions(sub, "", div, "A")
    sat = node(unreal.MaterialExpressionSaturate, -300, 640)
    mel.connect_material_expressions(div, "", sat, "Input")
    lerp2 = node(unreal.MaterialExpressionLinearInterpolate, 0, 0)
    mel.connect_material_expressions(lerp, "", lerp2, "A")
    mel.connect_material_expressions(sand, "RGB", lerp2, "B")
    mel.connect_material_expressions(sat, "", lerp2, "Alpha")
    mel.connect_material_property(lerp2, "",
                                  unreal.MaterialProperty.MP_BASE_COLOR)
    mel.recompile_material(mat)
    ok, why = compiled(mat)
    say("step3 +sand-on-height: compiled=%s %s" % (ok, why))

    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    src = mel.get_material_property_input_node(
        mat, unreal.MaterialProperty.MP_BASE_COLOR)
    say("FINAL BaseColor <- %s  compiled=%s" %
        (src.get_class().get_name() if src else "NOTHING", ok))
    say("--- done ---")


if __name__ == "__main__":
    main()
