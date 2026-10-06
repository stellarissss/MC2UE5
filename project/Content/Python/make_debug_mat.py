# -*- coding: utf-8 -*-
"""A flat magenta material, to separate 'texture is white' from 'material unused'."""
import unreal
OUT="Q:/MC2UE5/logs/make_debug_mat.txt"; L=[]
def say(s):
    L.append(str(s)); open(OUT,"w").write("\n".join(L)+"\n")
P="/Game/MC/CC0/MC_Debug"
if unreal.EditorAssetLibrary.does_asset_exist(P):
    m=unreal.load_asset(P)
else:
    m=unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "MC_Debug","/Game/MC/CC0",unreal.Material,unreal.MaterialFactoryNew())
mel=unreal.MaterialEditingLibrary
for e in mel.get_material_expressions(m):
    mel.delete_material_expression(m,e)
c=mel.create_material_expression(m,unreal.MaterialExpressionConstant3Vector,-400,0)
c.set_editor_property("constant", unreal.LinearColor(1.0,0.0,1.0,1.0))
mel.connect_material_property(c,"",unreal.MaterialProperty.MP_BASE_COLOR)
r=mel.create_material_expression(m,unreal.MaterialExpressionConstant,-400,250)
r.set_editor_property("r",0.8)
mel.connect_material_property(r,"",unreal.MaterialProperty.MP_ROUGHNESS)
mel.recompile_material(m)
unreal.EditorAssetLibrary.save_loaded_asset(m)
say("debug material ready: %s" % P)
say("--- done ---")
