# -*- coding: utf-8 -*-
"""
spawn_vantage.py -- put the player where the campus actually is.

The player spawned at world (0, 0), which is inside the surveyed campus
rectangle but on empty ground: the built content sits around block
(100..200, -300..-200) and the three classified buildings are at depth -500.
From (0, 0) the nearest prop is 68 m away and out of view, so a fresh launch
showed nothing but grass -- "the school cannot be seen".

This moves the PlayerStart to the middle of the densest structural cell and aims
it at the buildings. The character's own BeginPlay still snaps it onto the
collision surface, so the Z here only has to be roughly right.

Everything is guarded and reported step by step: a bare Python exception inside
-ExecutePythonScript takes the editor down without writing anything, which has
cost several runs already.

Run inside the editor.
"""

import traceback

import unreal

OUT = "Q:/MC2UE5/logs/spawn_vantage.txt"
SPAWN_CM = (15000.0, -25000.0, 600.0)   # middle of the densest structural cell
YAW = 261.0                             # toward the buildings at (11000, -49500)
_lines = []


def say(m):
    _lines.append(str(m))
    try:
        unreal.log("[MCSPAWN] " + str(m))
    except Exception:
        pass
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


def move(actor, loc, rot):
    """
    Move and turn an actor, trying the APIs in order of preference.

    ``EditorLevelLibrary.set_actor_location`` does not exist in 5.8, and the
    reflected Actor method is the one that does; the transform fallback covers
    the case where neither is bound. Returns the name of the call that worked.
    """
    for how in ("actor_method", "transform"):
        try:
            if how == "actor_method":
                actor.set_actor_location(loc, False, True)
                actor.set_actor_rotation(rot, False)
            else:
                actor.set_actor_transform(
                    unreal.Transform(loc, rot, unreal.Vector(1, 1, 1)),
                    False, True)
            return how
        except Exception as exc:
            say("  move via %s failed: %s" % (how, str(exc)[:70]))
    return None


def main():
    say("start")
    try:
        w = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        say("world=%s" % ("yes" if w else "NO"))
        if w is None:
            return

        starts = unreal.GameplayStatics.get_all_actors_of_class(
            w, unreal.PlayerStart)
        say("PlayerStart actors: %d" % len(starts))
        if not starts:
            return

        target = starts[0]
        loc = unreal.Vector(SPAWN_CM[0], SPAWN_CM[1], SPAWN_CM[2])
        # Keyword form on purpose: FRotator's positional order in the Python
        # bindings is not (pitch, yaw, roll), and packing yaw into the second
        # slot silently produced yaw 180 instead of 261.
        rot = unreal.Rotator(pitch=0.0, yaw=YAW, roll=0.0)
        say("wanted rot: p=%.0f y=%.0f r=%.0f"
            % (rot.pitch, rot.yaw, rot.roll))

        how = move(target, loc, rot)
        got = target.get_actor_location()
        gotr = target.get_actor_rotation()
        say("PlayerStart -> (%.0f, %.0f, %.0f) yaw=%.0f  (via %s)"
            % (got.x, got.y, got.z, gotr.yaw, how))

        # Keep the preview pawn with the start so an editor session matches.
        pawns = unreal.GameplayStatics.get_all_actors_of_class(
            w, unreal.MCReplicaCharacter)
        say("MCReplicaCharacter actors: %d" % len(pawns))
        for p in pawns:
            move(p, loc, rot)
        say("moved %d preview pawn(s)" % len(pawns))

        # Save the map, not just "dirty packages". A moved actor does not
        # always mark the level package dirty, and save_dirty_packages then
        # writes nothing -- which is why an earlier run reported the move and a
        # later session still found the PlayerStart at its old transform.
        for how in ("save_current_level",):
            try:
                unreal.EditorLevelLibrary.save_current_level()
                say("saved via %s" % how)
            except Exception as exc:
                say("save via %s failed: %s" % (how, str(exc)[:70]))
        try:
            say("save_asset -> %s" % unreal.EditorAssetLibrary.save_asset(
                "/Game/Maps/MCReplica", only_if_is_dirty=False))
        except Exception as exc:
            say("save_asset failed: %s" % str(exc)[:70])
    except Exception:
        say("FAILED:\n%s" % traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
