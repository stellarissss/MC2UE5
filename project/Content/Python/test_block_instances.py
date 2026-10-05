# -*- coding: utf-8 -*-
"""
test_block_instances.py -- can Python hand a bulk byte buffer to the C++ cluster?

Decides the assembly design: if ``bytes`` marshals to ``TArray<uint8>``, the
Python assembler can feed packed blocks straight into the C++ cluster and skip
1.17M Python Transform objects entirely. If it does not, the assembler has to
write the buffer to a file for C++ to read.

Also verifies the axis mapping by reading back an instance transform: height must
land on Z, depth on Y.
"""
import struct
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/test_block_instances.txt"
L = []


def w(s):
    L.append(str(s))
    unreal.log("[MCBLK] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


try:
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    w("EditorActorSubsystem ok")

    # Three blocks: (lx, lz, y) = (1,2,3), (0,0,0), (5,7,9)
    recs = [(1, 2, 3), (0, 0, 0), (5, 7, 9)]
    packed = b"".join(struct.pack("<HHH", *r) for r in recs)
    w("packed %d bytes for %d blocks" % (len(packed), len(recs)))

    mesh = unreal.load_asset("/Game/Meshes/Cube1x1x1")
    w("mesh: %s" % (mesh.get_name() if mesh else "MISSING"))

    actor = eas.spawn_actor_from_class(unreal.MCReplicaPropCluster,
                                       unreal.Vector(0, 0, 0),
                                       unreal.Rotator(0, 0, 0))
    w("spawned cluster: %s" % (actor is not None))
    if actor is None:
        w("--- done ---")
        raise SystemExit

    actor.set_mesh(mesh)

    ok = False
    for how, arg in (("bytes", packed),
                     ("bytearray", bytearray(packed)),
                     ("list", list(packed))):
        try:
            actor.clear_instances() if hasattr(actor, "clear_instances") else None
            n = actor.add_block_instances(arg, 0.0)
            w("marshalling via %-9s -> returned %s" % (how, n))
            if n:
                ok = True
                w("  instance_count now: %d" % actor.get_instance_count())
                break
        except Exception as exc:
            w("marshalling via %-9s FAILED: %s" % (how, str(exc)[:150]))

    if ok:
        # Read back the first transform to confirm the axis mapping.
        comp = actor.get_editor_property("instances")
        w("component: %s" % (comp.get_name() if comp else "NONE"))
        n = comp.get_instance_count()
        w("component instance count: %d" % n)
        for i in range(min(n, 3)):
            t = comp.get_instance_transform(i, True)
            loc = t.translation
            w("  inst %d world=(%.1f, %.1f, %.1f)" % (i, loc.x, loc.y, loc.z))
        w("EXPECT for block (1,2,3): x=150 y=250 z=350  (x, depth, height)")

    eas.destroy_actor(actor)
except Exception:
    w("FAILED:/n%s" % traceback.format_exc())
w("--- done ---")
