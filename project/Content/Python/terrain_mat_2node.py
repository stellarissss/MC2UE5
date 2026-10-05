# -*- coding: utf-8 -*-
"""Terrain material in the shape that is known to compile (see MC_Character)."""
import unreal
OUT = "Q:/MC2UE5/logs/terrain_mat_2node.txt"
path = "/Game/MC/Materials/MC_Terrain"
if unreal.EditorAssetLibrary.does_asset_exist(path):
    unreal.EditorAssetLibrary.delete_asset(path)
mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
    "MC_Terrain", "/Game/MC/Materials", unreal.Material,
    unreal.MaterialFactoryNew())
mel = unreal.MaterialEditingLibrary
s = mel.create_material_expression(
    mat, unreal.MaterialExpressionTextureSampleParameter2D, -400, 0)
s.set_editor_property("ParameterName", "grass_block_top")
t = unreal.load_asset("/Game/MC/Textures/grass_block_top")
s.set_editor_property("Texture", t)
mel.connect_material_property(s, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
c = mel.create_material_expression(
    mat, unreal.MaterialExpressionConstant, -200, 200)
c.set_editor_property("R", 0.9)
mel.connect_material_property(c, "", unreal.MaterialProperty.MP_ROUGHNESS)
mel.recompile_material(mat)
unreal.EditorAssetLibrary.save_loaded_asset(mat)
node = mel.get_material_property_input_node(mat, unreal.MaterialProperty.MP_BASE_COLOR)
open(OUT, "w").write("BaseColor <- %s ; tex=%s\n" % (
    node.get_class().get_name() if node else "NOTHING", t is not None))
