# -*- coding: utf-8 -*-
"""Re-import the grass texture with its missed biome tint applied."""
import unreal

SRC = "Q:/MC2UE5/textures_tinted/grass_block_top.png"
DEST_DIR = "/Game/MC/Textures"
OUT = "Q:/MC2UE5/logs/reimport_grass.txt"

tools = unreal.AssetToolsHelpers.get_asset_tools()
task = unreal.AssetImportTask()
task.set_editor_property("filename", SRC)
task.set_editor_property("destination_path", DEST_DIR)
task.set_editor_property("destination_name", "grass_block_top")
task.set_editor_property("automated", True)
task.set_editor_property("replace_existing", True)
task.set_editor_property("save", True)
tools.import_asset_tasks([task])
tex = unreal.load_asset("%s/grass_block_top" % DEST_DIR)
if tex is not None:
    try:
        tex.set_editor_property("filter", unreal.TextureFilter.TF_NEAREST)
        tex.set_editor_property("srgb", True)
    except Exception:
        pass
    unreal.EditorAssetLibrary.save_loaded_asset(tex)
open(OUT, "w").write("grass re-imported: %s\n" % (tex is not None))
unreal.log("[MCGRASS] done")
