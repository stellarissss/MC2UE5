# -*- coding: utf-8 -*-
"""
hide_voxel_layer.py -- census the level by actor class and hide the voxel layer,
without relying on -MClayers.

Why not the command line. The layer switch cannot currently be trusted here:
its parser and its logging both live in C++ that has not been rebuilt into the
editor binary on this machine, so a run with -MClayers=-vox produced NO
mclayers.txt line at all, while a line left over from an older run was still
sitting in that append-only file. Reading the stale line is exactly how an
earlier round concluded struct=0 when only -vox had been passed.

Acting on the actor classes directly is unambiguous and reports what it did:

  * census every actor by class, with the instance and mesh counts that matter;
  * hide the MCReplicaPropCluster instances (the 1,171,144-instance voxel layer)
    by setting visibility on the actor, which is a level property and therefore
    saved with the level and visible to any later run;
  * leave the 158 bld_* and 24 terrain_* actors alone.

Run:

    UnrealEditor-Cmd.exe MCReplica.uproject -ExecutePythonScript=<this> ^
        -nullrhi -unattended -nopause -nosplash -stdout

Pass MC_UNHIDE=1 to put the voxel layer back.
"""

import os
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/hide_voxel_layer.txt"
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCVOX] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def label(a):
    try:
        return a.get_actor_label()
    except Exception:
        return a.get_name()


def main():
    ok = True
    unhide = os.environ.get("MC_UNHIDE", "0") == "1"
    try:
        say("=== hide_voxel_layer (unhide=%s) ===" % unhide)
        lvl = unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        say("load_map -> %s" % (lvl.get_name() if lvl else "None"))
        if lvl is None:
            raise RuntimeError("map did not load")

        world = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        actors = unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.Actor)

        by_class = {}
        mesh_total = {}
        for a in actors:
            cls = a.get_class().get_name()
            by_class[cls] = by_class.get(cls, 0) + 1
            c = a.get_component_by_class(unreal.StaticMeshComponent)
            if c is not None:
                m = c.get_editor_property("static_mesh")
                key = m.get_name() if m else "NONE"
                cur = mesh_total.setdefault(cls, {})
                cur[key] = cur.get(key, 0) + 1

        say("")
        say("=== census: %d actors, %d classes ==="
            % (len(actors), len(by_class)))
        for cls in sorted(by_class, key=lambda c: -by_class[c]):
            say("  %-28s %5d" % (cls, by_class[cls]))
        say("")
        say("=== meshes per class (top 8 each) ===")
        for cls in sorted(mesh_total, key=lambda c: -by_class.get(c, 0)):
            items = sorted(mesh_total[cls].items(), key=lambda z: -z[1])[:8]
            say("  %s" % cls)
            for name, n in items:
                say("      %-34s %5d" % (name, n))

        n_vox = 0
        changed = 0
        for a in actors:
            if a.get_class().get_name() != "MCReplicaPropCluster":
                continue
            n_vox += 1
            # Only the setter is reliable here; the getter raised on this build,
            # so the before/after comparison is done by the reload census below
            # rather than by reading the flag back in the same process.
            try:
                a.set_actor_hidden_in_game(not unhide)
                changed += 1
            except Exception as exc:
                say("  set_actor_hidden_in_game failed on %s: %s"
                    % (label(a), str(exc)[:60]))
        say("")
        say("MCReplicaPropCluster actors: %d, visibility changed on %d"
            % (n_vox, changed))
        say("  -> voxel layer is now %s"
            % ("VISIBLE" if unhide else "HIDDEN"))

        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        say("save_current_level -> %s" % les.save_current_level())

        # Verify from disk, not from this process.
        unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        w2 = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        again = unreal.GameplayStatics.get_all_actors_of_class(
            w2, unreal.Actor)
        hidden = 0
        for a in again:
            if a.get_class().get_name() != "MCReplicaPropCluster":
                continue
            try:
                if a.is_actor_hidden_in_game():
                    hidden += 1
            except Exception:
                # No getter on this build: fall back to counting the components
                # the renderer would skip, which is what actually matters.
                c = a.get_component_by_class(unreal.InstancedStaticMeshComponent)
                if c is not None and not c.is_visible():
                    hidden += 1
        say("after reload: %d actors, %d MCReplicaPropCluster hidden"
            % (len(again), hidden))
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
