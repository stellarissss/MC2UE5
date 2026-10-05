import unreal
OUT = "Q:/MC2UE5/logs/probe2.txt"
L = []
def w(s): L.append(str(s)); open(OUT, "w").write("\n".join(L) + "\n")
EAS = unreal.EditorActorSubsystem
w("EditorActorSubsystem:")
for m in dir(EAS):
    if not m.startswith("__"):
        w("   " + m)
w("")
w("Actor has set_root_component: %s" % hasattr(unreal.Actor, "set_root_component"))
w("Actor has add_component: %s" % hasattr(unreal.Actor, "add_component"))
w("HISM has register_component: %s" % hasattr(unreal.HierarchicalInstancedStaticMeshComponent, "register_component"))
w("HISM has set_editor_property: %s" % hasattr(unreal.HierarchicalInstancedStaticMeshComponent, "set_editor_property"))
