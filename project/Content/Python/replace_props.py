# -*- coding: utf-8 -*-
"""
replace_props.py -- re-place the campus props with the corrected axis mapping.

Only the props are rebuilt. Re-running the whole level builder would also
recreate the terrain meshes and the terrain material, and the terrain material
is currently a hand-verified minimal graph that has to be left alone; the props
are independent of it.

Deletes the existing prop clusters and calls build_release_level.build_props(),
so the mesh recipes, materials, culling distances and cell grouping all stay
exactly as the canonical builder defines them -- only the transform axes differ.

Run inside the editor.
"""

import sys
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/replace_props.txt"
_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCPROPS] " + str(m))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    say("start")
    # Imported here, not at module scope: a failure in the builder module must
    # surface in this report rather than killing the editor before it can write
    # anything at all.
    sys.path.insert(0, r"Q:\MC2UE5\repo\project\Content\Python")
    try:
        import build_release_level as B
        say("imported build_release_level")
    except Exception:
        say("IMPORT FAILED:\n%s" % traceback.format_exc())
        return

    # ---- remove the old clusters ----------------------------------------
    # Collect first, then destroy: destroying an actor while walking the level's
    # actor list invalidates the list under the iteration and takes the editor
    # down before anything can be reported.
    doomed = []
    for a in unreal.EditorLevelLibrary.get_all_level_actors():
        cls = a.get_class().get_name()
        label = ""
        try:
            label = a.get_actor_label()
        except Exception:
            pass
        if cls == "MCReplicaPropCluster" or label.startswith("Props_"):
            doomed.append(a)
    say("found %d old prop actors to remove" % len(doomed))
    removed = 0
    for a in doomed:
        try:
            # ACharacter/AActor in the Python bindings exposes no plain
            # ``destroy``; teardown goes through the editor level library.
            unreal.EditorLevelLibrary.destroy_actor(a)
            removed += 1
        except Exception as exc:
            say("  destroy failed: %s" % str(exc)[:80])
    say("removed %d" % removed)

    # ---- rebuild with the corrected transforms ---------------------------
    B.IMPORT_PROPS = True
    try:
        root = B.resolve_root()
        say("root=%s" % root)
        total = B.build_props(root)
        say("build_props -> %d instances" % total)
    except Exception:
        say("BUILD PROPS FAILED:\n%s" % traceback.format_exc())
        return

    try:
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        say("saved")
    except Exception as exc:
        say("save failed: %s" % exc)

    # ---- report where they landed ---------------------------------------
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    zs = []
    for a in actors:
        if a.get_class().get_name() != "MCReplicaPropCluster":
            continue
        for c in a.get_components_by_class(unreal.InstancedStaticMeshComponent):
            for i in range(c.get_instance_count()):
                t = c.get_instance_transform(i, True)
                if t is not None:
                    try:
                        zs.append(t.translation.z)
                    except Exception:
                        pass
    if zs:
        say("instance Z range: %.0f .. %.0f cm (mean %.0f)"
            % (min(zs), max(zs), sum(zs) / len(zs)))
    else:
        say("could not read instance Z values")
    say("--- done ---")


if __name__ == "__main__":
    main()
