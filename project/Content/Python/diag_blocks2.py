# -*- coding: utf-8 -*-
"""Check whether the CC0 material reached the block component."""
import traceback
import unreal

OUT = "Q:/MC2UE5/logs/diag_blocks2.txt"
L = []
def say(s):
    L.append(str(s)); unreal.log("[MCDIAG2] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")

try:
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    sample = None
    for a in actors:
        if a.get_class().get_name() == "MCReplicaPropCluster":
            sample = a
            break
    if sample is None:
        say("no cluster")
    else:
        say("cluster: %s  instances=%d" % (sample.get_actor_label(),
                                           sample.get_instance_count()))
        comp = sample.get_editor_property("instances")
        say("comp: %s" % (comp.get_name() if comp else "NONE"))
        # HISM in Python exposes static_mesh as an editor property, not a getter.
        try:
            sm = comp.get_editor_property("static_mesh")
            say("static_mesh: %s" % (sm.get_name() if sm else "NONE"))
        except Exception as e:
            say("static_mesh read failed: %s" % str(e)[:80])
        n = comp.get_num_materials()
        say("num materials: %d" % n)
        for i in range(n):
            m = comp.get_material(i)
            say("  material[%d]: %s" % (i, m.get_name() if m else "NONE"))
            if m:
                say("     class=%s" % m.get_class().get_name())
                try:
                    p = m.get_editor_property("parent")
                    say("     parent=%s" % (p.get_name() if p else "NONE"))
                except Exception as e:
                    say("     parent read failed: %s" % str(e)[:60])
        say("cull: start=%s end=%s" % (
            comp.get_editor_property("instance_start_cull_distance"),
            comp.get_editor_property("instance_end_cull_distance")))
        say("collision enabled: %s" % comp.get_editor_property("body_instance"))
except Exception:
    say("FAILED:/n" + traceback.format_exc())
say("--- done ---")
