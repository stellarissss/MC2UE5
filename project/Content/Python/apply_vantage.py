# -*- coding: utf-8 -*-
"""Place PlayerStart at the vantage tool/pick_vantage.py selected."""
import json
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/apply_vantage.txt"
VANTAGE = "Q:/MC2UE5/logs/vantage.json"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCSPAWN2] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


try:
    v = json.load(open(VANTAGE))
    say("vantage: spawn_block=%s surface=%s yaw=%s"
        % (v["spawn_block"], v["surface_block"], v["yaw"]))

    w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    starts = unreal.GameplayStatics.get_all_actors_of_class(w, unreal.PlayerStart)
    say("PlayerStart actors: %d" % len(starts))
    if not starts:
        raise SystemExit

    loc = unreal.Vector(v["spawn_cm"][0], v["spawn_cm"][1], v["spawn_cm"][2])
    rot = unreal.Rotator(pitch=0.0, yaw=v["yaw"], roll=0.0)
    # Keyword rotation on purpose: the positional order is not (pitch, yaw,
    # roll) in the bindings and packing yaw second silently yields the wrong
    # heading.
    t = starts[0]
    t.set_actor_location(loc, False, True)
    t.set_actor_rotation(rot, False)
    got = t.get_actor_location()
    say("PlayerStart -> (%.0f, %.0f, %.0f) yaw=%.1f"
        % (got.x, got.y, got.z, t.get_actor_rotation().yaw))

    for p in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.MCReplicaCharacter):
        p.set_actor_location(loc, False, True)
        p.set_actor_rotation(rot, False)
    say("moved preview pawn")

    unreal.EditorLevelLibrary.save_current_level()
    say("saved; save_asset -> %s" % unreal.EditorAssetLibrary.save_asset(
        "/Game/Maps/MCReplica", only_if_is_dirty=False))
except Exception:
    say("FAILED:/n%s" % traceback.format_exc())
say("--- done ---")
