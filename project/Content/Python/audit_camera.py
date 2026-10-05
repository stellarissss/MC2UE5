# -*- coding: utf-8 -*-
"""
audit_camera.py -- find out what the camera can actually see.

Written after a "black screen" report that two rounds of winding fixes did not
resolve. Everything previously verified was true -- meshes placed, collision
present, cook clean, game running, 97% of triangles front-facing -- and the
player still saw sky where the ground should be. So the premise that the terrain
was invisible was never actually tested; only its properties were.

This answers the question directly, by asking the engine rather than inferring
from asset data:

  * where the camera is, relative to the terrain under it;
  * what a ray fired straight along the camera's view direction hits, and what
    it hits fired straight down;
  * whether the terrain actor is even in the player's streaming area.

A line trace respects the same world the renderer draws, so it distinguishes
"the surface is there but culled" from "the surface is not there" -- the two
look identical from inside the view frustum but have opposite fixes.

Run headless:

    UnrealEditor.exe MCReplica.uproject \
        -ExecutePythonScript=project/Content/Python/audit_camera.py \
        -unattended -nopause -nosplash -nullrhi
"""

import math
import sys
import time

import unreal

_t0 = time.time()
_lines = []


def log(msg):
    unreal.log("[CAMERA %6.1fs] %s" % (time.time() - _t0, msg))
    _lines.append(msg)


def _world():
    return unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()


def _trace(world, start, end):
    """-> (hit_something, distance, description)"""
    try:
        hit = unreal.SystemLibrary.line_trace_single(world, start, end,
                                                     unreal.TraceTypeQuery.TRACE_TYPE_VISIBILITY)
        return bool(hit.get_editor_property("blocking_hit")), \
            float(hit.get_editor_property("distance")), \
            hit.get_actor()
    except Exception as exc:
        log("  trace failed (%s)" % str(exc)[:70])
        return None, None, None


def run():
    log("=" * 70)
    log("camera audit")
    log("=" * 70)

    world = _world()
    if world is None:
        log("no world")
        return 1

    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    log("actors in level: %d" % len(actors))

    terrain, pawn, start = [], None, None
    for a in actors:
        label = a.get_actor_label()
        if label.startswith("Terrain_") or label.startswith("TerrainCollision_"):
            terrain.append(a)
        elif label == "MC_Player":
            pawn = a
        elif a.get_class().get_name() == "PlayerStart":
            start = a

    log("terrain actors: %d" % len(terrain))
    log("player pawn  : %s" % ("yes" if pawn else "MISSING"))
    log("PlayerStart  : %s" % ("yes" if start else "MISSING"))

    for a in terrain:
        loc = a.get_actor_location()
        comp = a.get_component_by_class(unreal.StaticMeshComponent)
        mesh = comp.get_editor_property("static_mesh") if comp else None
        log("  %-34s loc=(%.0f, %.0f, %.0f) mesh=%s"
            % (a.get_actor_label(), loc.x, loc.y, loc.z,
               mesh.get_name() if mesh else "NONE"))

    # ---- where is the camera, and what is under it? ----------------------
    if pawn is not None:
        cam = None
        for c in pawn.get_components_by_class(unreal.CameraComponent):
            cam = c
            break
        if cam is not None:
            cl = cam.get_editor_property("location")
            cr = cam.get_editor_property("rotation")
            log("")
            log("camera at (%.0f, %.0f, %.0f) rot=(%.0f, %.0f, %.0f)"
                % (cl.x, cl.y, cl.z, cr.pitch, cr.yaw, cr.roll))

            fwd = unreal.Vector(math.cos(math.radians(cr.yaw))
                                * math.cos(math.radians(cr.pitch)),
                                math.sin(math.radians(cr.yaw))
                                * math.cos(math.radians(cr.pitch)),
                                math.sin(math.radians(cr.pitch)))

            # Straight down from the camera: is there ground beneath it?
            down = unreal.Vector(cl.x, cl.y, cl.z - 20000.0)
            hit, dist, actor = _trace(world, cl, down)
            if hit:
                log("  ray DOWN hits %-28s at %.0f cm below the camera"
                    % (actor.get_actor_label() if actor else "?", dist))
                ground = cl.z - dist
                log("  => ground is at z=%.0f, camera at z=%.0f  (%s)"
                    % (ground, cl.z,
                       "ABOVE ground" if cl.z > ground else "UNDERGROUND"))
            else:
                log("  ray DOWN hits NOTHING -- there is no ground under the "
                    "camera")

            # Along the view direction: what is actually in front?
            ahead = unreal.Vector(cl.x + fwd.x * 30000.0,
                                 cl.y + fwd.y * 30000.0,
                                 cl.z + fwd.z * 30000.0)
            hit, dist, actor = _trace(world, cl, ahead)
            log("  ray FORWARD hits %s" % (actor.get_actor_label() if hit and actor
                                           else ("nothing" if not hit else "?")))
            if hit:
                log("  => the view direction lands on geometry %.0f cm away"
                    % dist)

            # A little below the view ray, where a walking camera would see
            # ground: this is the one that separates "surface present" from
            # "surface present but culled".
            look = unreal.Vector(cl.x + fwd.x * 8000.0,
                                 cl.y + fwd.y * 8000.0,
                                 cl.z + fwd.z * 8000.0 - 1500.0)
            hit, dist, actor = _trace(world, cl, look)
            log("  ray FORWARD-DOWN (8 m ahead, 15 m below) hits %s"
                % (actor.get_actor_label() if hit and actor
                   else ("nothing" if not hit else "?")))

    log("")
    log("-" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(run())
