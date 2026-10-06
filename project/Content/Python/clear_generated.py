# -*- coding: utf-8 -*-
"""
clear_generated.py -- remove the placeholder level content before the voxel build.

The level currently holds the earlier generated stand-ins: the runtime-terrain
meshes (`Terrain_*`, `TerrainCollision_*`) and the 1297 primitive prop clusters
(`Props_*`). The voxel block layer supersedes all of them -- keeping them would
put overlapping geometry in the same space and z-fight.

The lighting (MC_Sun / MC_SkyLight / SkyAtmosphere / MC_Fog) and the player
(PlayerStart / MC_Player) are deliberately kept.

Collect-then-destroy: destroying during iteration invalidates the level's actor
list and takes the editor down without output.
"""

import traceback

import unreal

OUT = "Q:/MC2UE5/logs/clear_generated.txt"
PREFIXES = ("Terrain_", "TerrainCollision_", "Props_", "MCblk_")
_lines = []


def say(m):
    _lines.append(str(m))
    try:
        unreal.log("[MCCLEAR] " + str(m))
    except Exception:
        pass
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


def main():
    say("start")
    try:
        w = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        if w is None:
            say("no editor world")
            return
        actors = unreal.EditorLevelLibrary.get_all_level_actors()
        say("actors before: %d" % len(actors))

        doomed = []
        for a in actors:
            try:
                label = a.get_actor_label()
            except Exception:
                continue
            if label.startswith(PREFIXES):
                doomed.append((label, a))
        say("to remove: %d" % len(doomed))

        removed = 0
        for label, a in doomed:
            try:
                unreal.EditorLevelLibrary.destroy_actor(a)
                removed += 1
            except Exception as exc:
                say("  destroy failed for %s: %s" % (label, str(exc)[:60]))
        say("removed %d" % removed)

        after = unreal.EditorLevelLibrary.get_all_level_actors()
        say("actors after: %d" % len(after))
        for a in after:
            try:
                say("  kept: %s (%s)" % (a.get_actor_label(),
                                         a.get_class().get_name()))
            except Exception:
                pass

        unreal.EditorLevelLibrary.save_current_level()
        say("saved; save_asset -> %s" % unreal.EditorAssetLibrary.save_asset(
            "/Game/Maps/MCReplica", only_if_is_dirty=False))
    except Exception:
        say("FAILED:/n%s" % traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
