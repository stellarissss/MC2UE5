# -*- coding: utf-8 -*-
"""
verify_spawn.py -- read back the spawn point and force the level to be saved.

spawn_vantage.py reported the PlayerStart moved, but the packaged game still
spawned the pawn at the old spot, which means the change did not reach the map
asset rather than that the move failed. ``save_dirty_packages`` only saves
packages the editor considers dirty, so this checks the actor's actual transform
and then saves the level explicitly.

Run inside the editor.
"""

import traceback

import unreal

OUT = "Q:/MC2UE5/logs/verify_spawn.txt"
_lines = []


def say(m):
    _lines.append(str(m))
    try:
        unreal.log("[MCVERIFY] " + str(m))
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
        say("world=%s" % (w.get_name() if w else "NO"))
        if w is None:
            return

        for cls, label in ((unreal.PlayerStart, "PlayerStart"),
                           (unreal.MCReplicaCharacter, "MCReplicaCharacter")):
            actors = unreal.GameplayStatics.get_all_actors_of_class(w, cls)
            say("%s: %d" % (label, len(actors)))
            for a in actors:
                loc = a.get_actor_location()
                rot = a.get_actor_rotation()
                say("   loc=(%.0f, %.0f, %.0f) yaw=%.0f"
                    % (loc.x, loc.y, loc.z, rot.yaw))

        # ---- force a save -------------------------------------------------
        # Both spellings, and the level asset directly: whichever is bound,
        # the map on disk ends up matching what is in memory.
        for how in ("save_current_level", "save_loaded_assets"):
            try:
                if how == "save_current_level":
                    unreal.EditorLevelLibrary.save_current_level()
                else:
                    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(
                        True, True)
                say("saved via %s" % how)
            except Exception as exc:
                say("save via %s failed: %s" % (how, str(exc)[:70]))

        try:
            ok = unreal.EditorAssetLibrary.save_asset(
                "/Game/Maps/MCReplica", only_if_is_dirty=False)
            say("save_asset(/Game/Maps/MCReplica) -> %s" % ok)
        except Exception as exc:
            say("save_asset failed: %s" % str(exc)[:70])
    except Exception:
        say("FAILED:\n%s" % traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
