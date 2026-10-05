# -*- coding: utf-8 -*-
"""List the Actor/HISM APIs available in this engine build, to stop guessing."""
import unreal
OUT = "Q:/MC2UE5/logs/probe_actor_api.txt"
L = []
def w(s): L.append(str(s)); open(OUT, "w").write("\n".join(L) + "\n")
w("unreal.new_object exists: %s" % hasattr(unreal, "new_object"))
A = unreal.Actor
meth = [m for m in dir(A) if not m.startswith("__")]
w("Actor methods (%d):" % len(meth))
for m in meth:
    if any(k in m.lower() for k in ("component", "instance", "root", "attach")):
        w("   " + m)
H = unreal.HierarchicalInstancedStaticMeshComponent
hm = [m for m in dir(H) if not m.startswith("__")]
w("")
w("HISM instance/register/attach methods:")
for m in hm:
    if any(k in m.lower() for k in ("instance", "register", "attach", "mesh", "material", "mobility", "cull")):
        w("   " + m)
B = dir(unreal)
w("")
w("unreal-level helpers:")
for m in B:
    if any(k in m.lower() for k in ("new_object", "subsystem")):
        w("   " + m)
