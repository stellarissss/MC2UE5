# -*- coding: utf-8 -*-
"""Rebuild the prop materials with a Constant3Vector tint."""
import traceback
import unreal
OUT = "Q:/MC2UE5/logs/prop_mats.txt"
lines = []
def say(m):
    lines.append(str(m)); unreal.log("[MCPM] " + str(m))
    open(OUT, "w").write("\n".join(lines) + "\n")
try:
    import sys
    sys.path.insert(0, r"Q:/MC2UE5/repo/project/Content/Python")
    import build_release_level as B
    say("imported")
    mats = B._prop_materials()
    mel = unreal.MaterialEditingLibrary
    for name, mat in sorted(mats.items()):
        if mat is None:
            say("%s: None" % name); continue
        src = mel.get_material_property_input_node(mat, unreal.MaterialProperty.MP_BASE_COLOR)
        say("%s <- %s" % (name, src.get_class().get_name() if src else "NOTHING"))
    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    say("saved")
except Exception:
    say("FAILED:/n" + traceback.format_exc())
say("--- done ---")
