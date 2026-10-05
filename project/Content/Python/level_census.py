# -*- coding: utf-8 -*-
"""
level_census.py -- detailed census of the built level.

audit_level.py gives the actor counts; this goes one level deeper, because the
count that matters is *instances*, not actors. The release notes claim 1297
props while only 26 prop actors exist, which is only consistent if each actor is
an instanced component holding many instances -- and that has to be checked
rather than assumed, since an instanced actor whose instance list came out empty
looks exactly like a populated one until something counts it.

Every per-actor probe is individually guarded: one unsupported property must not
cost the whole report.

Run inside the editor.
"""

import collections

import unreal

OUT = "Q:/MC2UE5/logs/level_census.txt"
_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCCENSUS] " + str(m))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def probe(a):
    """-> a short description of what this actor draws."""
    bits = [a.get_class().get_name()]

    try:
        ism = a.get_components_by_class(unreal.InstancedStaticMeshComponent)
    except Exception as exc:
        ism = []
        bits.append("ism-err:%s" % str(exc)[:40])
    if ism:
        counts, meshes, mats = [], set(), set()
        for c in ism:
            try:
                counts.append(c.get_instance_count())
            except Exception:
                counts.append(-1)
            try:
                m = c.get_static_mesh()
                meshes.add(m.get_name() if m else "<none>")
            except Exception:
                meshes.add("<err>")
            try:
                mm = c.get_material(0)
                mats.add(mm.get_name() if mm else "NONE")
            except Exception:
                mats.add("<err>")
        bits.append("ISM instances=%s meshes=%s mats=%s"
                    % (counts, sorted(meshes), sorted(mats)))
        return "  ".join(bits), sum(counts)

    try:
        smc = a.get_components_by_class(unreal.StaticMeshComponent)
    except Exception:
        smc = []
    if smc:
        try:
            m = smc[0].get_static_mesh()
            mm = smc[0].get_material(0)
            bits.append("STATIC mesh=%s mat=%s"
                        % (m.get_name() if m else "<none>",
                           mm.get_name() if mm else "NONE"))
        except Exception as exc:
            bits.append("static-err:%s" % str(exc)[:40])
    return "  ".join(bits), 0


def main():
    w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    say("map=%s" % (w.get_name() if w else "NO WORLD"))
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    say("actors=%d" % len(actors))

    by_label = collections.Counter()
    total_inst = 0
    say("")
    say("=== every actor ===")
    for a in actors:
        try:
            label = a.get_actor_label()
        except Exception:
            label = "<no label>"
        try:
            z = a.get_actor_location().z
        except Exception:
            z = float("nan")
        try:
            desc, inst = probe(a)
        except Exception as exc:
            desc, inst = "PROBE FAILED: %s" % str(exc)[:60], 0
        total_inst += inst
        by_label[label.split("_")[0]] += 1
        say("  %-26s z=%-9.0f %s" % (label[:26], z, desc))

    say("")
    say("=== summary ===")
    for k, n in by_label.most_common():
        say("  %-20s %d" % (k, n))
    say("TOTAL instanced instances: %d" % total_inst)
    say("--- done ---")


if __name__ == "__main__":
    main()
