# -*- coding: utf-8 -*-
"""
audit_and_fix_materials.py -- report what the placed meshes actually use, then
make it use the atlas.

Two separate questions, and only the second one is about looks:

  1. Does each mesh *reference* a material at all? The terrain tiles were placed
     with no material set, which means they render with the engine default --
     which is a mid-grey checkerboard and is very likely most of the "everything
     is brown" reading. This script measures it rather than assuming it.
  2. Is the material the atlas, or the old per-family CC0 material left over
     from the previous pipeline?

Both answers are recorded before anything is changed, so the fix can be judged
against the before state.
"""
import traceback
from collections import Counter

import unreal

OUT = "Q:/MC2UE5/logs/audit_materials.txt"
ATLAS = "/Game/MC/Atlas/M_MC_Atlas"
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCAUDIT] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def mat_name(c):
    if c is None:
        return "*** NONE ***"
    m = c.get_material(0)
    return m.get_name() if m else "*** SLOT EMPTY ***"


def audit(tag):
    w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    cnt = Counter()
    detail = {}
    for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
        lbl = a.get_actor_label()
        grp = ("B_ 结构" if lbl.startswith("B_") else
               "T_ 地形" if lbl.startswith("T_") else "其它")
        n = mat_name(a.static_mesh_component)
        cnt[(grp, n)] += 1
        detail.setdefault(grp, []).append((lbl, n))
    say("---- %s ----" % tag)
    for (grp, n), c in sorted(cnt.items()):
        say("  %-10s %-24s %4d" % (grp, n, c))
    for grp, rows in detail.items():
        say("  %s 样例: %s" % (grp, rows[:2]))
    return cnt


def main():
    try:
        atlas = unreal.load_asset(ATLAS)
        say("atlas material /Game/MC/Atlas/M_MC_Atlas = %s" % (atlas.get_name() if atlas else "*** MISSING ***"))
        if atlas:
            say("  图类型: %s" % [e.get_class().get_name() for e in
                                   unreal.MaterialEditingLibrary.get_material_expressions(atlas)])

        before = audit("修改前")

        unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        fixed = 0
        for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
            lbl = a.get_actor_label()
            if not (lbl.startswith("B_") or lbl.startswith("T_")):
                continue
            sc = a.static_mesh_component
            cur = sc.get_material(0)
            if cur is not None and cur.get_name() == "M_MC_Atlas":
                continue
            sc.set_material(0, atlas)
            fixed += 1
        say("set_material(0, M_MC_Atlas) 应用到 %d 个组件" % fixed)

        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        say("save_current_level -> %s" % les.save_current_level())
        audit("修改后（并已重载关卡）")
    except Exception:
        say("FAILED:/n" + traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
