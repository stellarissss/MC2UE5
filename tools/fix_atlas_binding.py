# -*- coding: utf-8 -*-
"""
fix_atlas_binding.py -- bind the atlas texture to the material's sampler.

Found by probing the material rather than by looking at renders: the
TextureSampleParameter2D named "Atlas" on M_MC_Atlas has `texture = None`.

An unbound texture sampler in UE returns **white**. That is the single most
plausible explanation for the whole project looking like a uniform cream/tan
wash no matter which atlas, tint, exposure or light value was tried: the
surfaces were never sampling anything at all. Every earlier colour fix was
tuning a value that was multiplied by white.

It survived because the importer deliberately reuses the existing material
rather than recreating it ("NOT recreating it"), so nothing ever re-bound the
texture after the material was authored.
"""
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/fix_atlas_binding.txt"
MAT = "/Game/MC/Atlas/M_MC_Atlas"
TEX = "/Game/MC/Atlas/T_MC_Atlas"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCBIND] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def main():
    try:
        mel = unreal.MaterialEditingLibrary
        mat = unreal.load_asset(MAT)
        tex = unreal.load_asset(TEX)
        say("material=%s  texture=%s" % (mat.get_name() if mat else None,
                                         tex.get_name() if tex else None))
        if mat is None or tex is None:
            say("*** 缺 material 或 texture，无法绑定")
            return

        before, after = [], []
        for e in mel.get_material_expressions(mat):
            if e.get_class().get_name() != "MaterialExpressionTextureSampleParameter2D":
                continue
            p = e.get_editor_property("parameter_name")
            cur = e.get_editor_property("texture")
            before.append((p, cur.get_name() if cur else None))
            e.set_editor_property("texture", tex)
            after.append((p, e.get_editor_property("texture").get_name()))

        say("绑定前: %s" % before)
        say("绑定后: %s" % after)

        mel.recompile_material(mat)
        unreal.EditorAssetLibrary.save_loaded_asset(mat)
        say("saved %s" % mat.get_name())

        # Read back from a fresh load: a set_editor_property that silently does
        # nothing has been the failure mode in this project more than once.
        m2 = unreal.load_asset(MAT)
        chk = [(e.get_editor_property("parameter_name"),
                (e.get_editor_property("texture").get_name()
                 if e.get_editor_property("texture") else None))
               for e in mel.get_material_expressions(m2)
               if e.get_class().get_name() == "MaterialExpressionTextureSampleParameter2D"]
        say("重载后核对: %s" % chk)
        say("绑定生效 = %s" % all(t == "T_MC_Atlas" for _, t in chk))
    except Exception:
        say("FAILED:/n" + traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
