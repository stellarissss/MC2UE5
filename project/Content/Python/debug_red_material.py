# -*- coding: utf-8 -*-
"""
debug_red_material.py -- prove whether the terrain meshes render at all.

The campus cannot be seen and everything reads as flat blue. From a screenshot
alone that is indistinguishable from "the terrain is not drawn" and "the terrain
is drawn but the material is wrong". This removes the ambiguity: it puts a
bright UNLIT red material on the four visual terrain meshes, so the next capture
is red if the geometry renders and unchanged if it does not.

    python debug_red_material.py            # install the debug material
    python debug_red_material.py --restore  # put MC_Terrain back

Run inside the editor.
"""

import sys

import unreal

MAT_DIR = "/Game/MC/Materials"
DEBUG_MAT = "/Game/MC/Materials/MC_DebugRed"
REAL_MAT = "/Game/MC/Materials/MC_Terrain"
TILES = ("T_overworld_00_00", "T_overworld_00_01",
         "T_overworld_01_00", "T_overworld_01_01")
REPORT = "Q:/MC2UE5/logs/debug_red.txt"
_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCRED] " + str(m))
    with open(REPORT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def make_debug_material():
    if unreal.EditorAssetLibrary.does_asset_exist(DEBUG_MAT):
        unreal.EditorAssetLibrary.delete_asset(DEBUG_MAT)
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "MC_DebugRed", MAT_DIR, unreal.Material, unreal.MaterialFactoryNew())
    mel = unreal.MaterialEditingLibrary
    node = mel.create_material_expression(
        mat, unreal.MaterialExpressionConstant3Vector, 0, 0)
    node.set_editor_property(
        "Constant", unreal.LinearColor(6.0, 0.0, 0.0, 1.0))
    # Unlit so the answer does not depend on any light being present.
    mat.set_editor_property("shading_model",
                            unreal.MaterialShadingModel.MSM_UNLIT)
    mel.connect_material_property(node, "",
                                  unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.connect_material_property(node, "",
                                  unreal.MaterialProperty.MP_BASE_COLOR)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    return mat


def main():
    restore = "--restore" in sys.argv
    mat = unreal.load_asset(REAL_MAT) if restore else make_debug_material()
    if mat is None:
        say("material unavailable")
        return

    for name in TILES:
        mesh = unreal.load_asset("/Game/MC/Terrain/%s" % name)
        if mesh is None:
            say("%s MISSING" % name)
            continue
        try:
            n = mesh.get_num_sections(0)
        except Exception:
            n = 1
        for i in range(max(1, n)):
            mesh.set_material(i, mat)
        unreal.EditorAssetLibrary.save_loaded_asset(mesh)
        say("%s material -> %s" % (name, mat.get_name()))

    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    say("--- done (%s) ---" % ("restore" if restore else "debug"))


if __name__ == "__main__":
    main()
