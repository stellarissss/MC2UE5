# -*- coding: utf-8 -*-
"""Inspect a block cluster: is the CC0 material actually on its component?"""
import traceback
import unreal

OUT = "Q:/MC2UE5/logs/diag_blocks.txt"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCDIAG] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


try:
    say("cube mesh: %s" % (
        unre := None) if False else "cube mesh: %s" % (
        unreal.load_asset("/Game/Meshes/Cube1x1x1").get_name()
        if unreal.load_asset("/Game/Meshes/Cube1x1x1") else "MISSING"))

    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    say("world actors: %d" % len(actors))
    kinds = {}
    sample = None
    for a in actors:
        k = a.get_class().get_name()
        kinds[k] = kinds.get(k, 0) + 1
        if k == "MCReplicaPropCluster" and sample is None:
            sample = a
    say("actor classes: %s" % kinds)
    if sample is None:
        say("no MCReplicaPropCluster in level")
    else:
        say("sample cluster: %s  instances=%d"
            % (sample.get_actor_label(), sample.get_instance_count()))
        comp = sample.get_editor_property("instances")
        if comp:
            sm = comp.get_static_mesh()
            say("  comp mesh: %s" % (sm.get_name() if sm else "NONE"))
            say("  num materials: %d" % comp.get_num_materials())
            for i in range(comp.get_num_materials()):
                m = comp.get_material(i)
                say("  comp material[%d]: %s" % (i, m.get_name() if m else "NONE"))
            say("  cull: start=%s end=%s" % (
                comp.get_editor_property("instance_start_cull_distance"),
                comp.get_editor_property("instance_end_cull_distance")))
        # Where does the player spawn, and is that inside a block?
        for a in actors:
            if a.get_class().get_name() == "PlayerStart":
                loc = a.get_actor_location()
                say("PlayerStart: (%.0f, %.0f, %.0f)" % (loc.x, loc.y, loc.z))
except Exception:
    say("FAILED:/n%s" % traceback.format_exc())
say("--- done ---")
