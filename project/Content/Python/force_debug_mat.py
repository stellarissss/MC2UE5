# -*- coding: utf-8 -*-
"""Put a flat magenta material on every block cluster -- the 1-bit experiment."""
import unreal
OUT="Q:/MC2UE5/logs/force_debug_mat.txt"; L=[]
def say(s):
    L.append(str(s)); open(OUT,"w").write("\n".join(L)+"\n")
m=unreal.load_asset("/Game/MC/CC0/MC_Debug")
say("debug mat: %s" % (m is not None))
n=0
for a in unreal.EditorLevelLibrary.get_all_level_actors():
    if a.get_class().get_name()!="MCReplicaPropCluster": continue
    c=a.get_editor_property("instances")
    if not c: continue
    c.set_material(0, m)
    n+=1
say("clusters set to debug material: %d" % n)
unreal.EditorLevelLibrary.save_current_level()
say("saved")
say("--- done ---")
