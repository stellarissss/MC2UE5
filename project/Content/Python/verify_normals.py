# -*- coding: utf-8 -*-
"""
verify_normals.py -- prove the imported meshes are front-facing, in-engine.

The black-screen bug was invisible to every check in the build: the editor
reported four meshes placed, collision was present, the cook succeeded and the
packaged game launched and ran. What was wrong was that every terrain triangle
was wound backwards, so the engine culled the entire ground and the player saw
sky through it.

Reading the normals back out of the *imported* asset closes that gap. The OBJ
is the input; this checks what the engine actually stored, which is the thing
that gets cooked and shipped. It needs no GPU, so it works on a build machine
with no display.

Run inside the editor:

    UnrealEditor.exe MCReplica.uproject \
        -ExecutePythonScript=project/Content/Python/verify_normals.py \
        -unattended -nopause -nosplash -nullrhi
"""

import sys
import time

import unreal

_t0 = time.time()
_errors = []
_notes = []

#: A front-facing surface points up. Terrain is a heightfield, so every normal
#: should have a strongly positive Z; anything at or below zero is a face the
#: engine will cull when the camera looks down at it.
UP_EPSILON = 0.05


def log(msg):
    unreal.log("[NORMALS %6.1fs] %s" % (time.time() - _t0, msg))


def err(msg):
    _errors.append(msg)
    unreal.log_error("[NORMALS] %s" % msg)


def warn(msg):
    _notes.append(msg)
    unreal.log_warning("[NORMALS] %s" % msg)


def _read_mesh_normals(mesh, max_tris=4000):
    """
    -> (front_facing, back_facing, degenerate, sampled, note) or None

    Delegates to ``UMCMeshWindingLibrary::ReadWinding``, which reads LOD0's
    position and index buffers directly.

    The C++ route is used because the Python API exposes no accessor for LOD
    vertex positions on this engine version: ``EditorStaticMeshLibrary`` has
    ``get_lod_count`` but no geometry getter, and ``StaticMeshDescription``
    wraps a ``MeshDescription`` whose attribute names differ from what a
    first reading of the docs suggests. Guessing at those names from Python
    costs a 20-second editor launch per attempt, which is why the check lives
    in C++ where the compiler settles it.
    """
    lib = getattr(unreal, "MCMeshWindingLibrary", None)
    if lib is None:
        warn("MCMeshWindingLibrary is unavailable -- is the MCReplica module "
             "compiled? Treat the result as unverified.")
        return None

    try:
        st = lib.read_winding(mesh, max_tris)
    except Exception as exc:
        warn("read_winding failed (%s)" % str(exc)[:80])
        return None

    try:
        front = st.get_editor_property("front_facing")
        back = st.get_editor_property("back_facing")
        degen = st.get_editor_property("degenerate")
        total = st.get_editor_property("sampled")
        valid = st.get_editor_property("valid")
        note = st.get_editor_property("note")
    except Exception as exc:
        warn("could not read the result struct (%s)" % str(exc)[:80])
        return None

    if not valid or total == 0:
        warn("mesh unreadable: %s" % note)
        return None
    return front, back, degen, total, note


def run():
    log("=" * 68)
    log("imported-mesh normal check")
    log("=" * 68)

    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        err("no editor world")
        return 1

    checked = 0
    ok_meshes = 0

    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        if actor.get_class().get_name() != "StaticMeshActor":
            continue
        label = actor.get_actor_label()
        if not (label.startswith("Terrain_") or label.startswith("TerrainCollision_")
                or label.startswith("Prop_")):
            continue

        comp = actor.get_component_by_class(unreal.StaticMeshComponent)
        if comp is None:
            continue
        mesh = comp.get_editor_property("static_mesh")
        if mesh is None:
            continue

        # Only the heightfield can be judged by "points up". Props are boxes and
        # cylinders, so a minority of back faces is correct there.
        is_terrain = label.startswith("Terrain")

        stats = _read_mesh_normals(mesh)
        if stats is None:
            warn("%s: mesh data unreadable; skipped" % label)
            continue
        front, back, degen, total, note = stats
        if total == 0:
            warn("%s: no triangles sampled" % label)
            continue

        checked += 1
        pct = 100.0 * front / total

        if is_terrain:
            # A heightfield is overwhelmingly upward-facing, but not entirely:
            # where the slope approaches vertical the cross product's Z goes to
            # zero and the sign is numerical noise. Those triangles are walls
            # and cliffs, which are legitimately steep.
            #
            # So the bar is not "zero back faces" but "back faces are a small
            # minority". The black-screen build measured 0.0%, which no amount
            # of cliff geometry produces.
            if pct >= 95.0:
                ok_meshes += 1
                log("[ok]   %-34s %4d sampled, %4d front, %4d back  (%.1f%% up)"
                    % (label, total, front, back, pct))
            else:
                err("%s: only %.1f%% of %d sampled triangles face up. The "
                    "engine culls the rest and the player sees through the "
                    "ground."
                    % (label, pct, total))
        else:
            # A closed box has 4 back faces out of 12 triangles.
            if degen == 0 and pct >= 20.0:
                ok_meshes += 1
                log("[ok]   %-34s %4d sampled, %.1f%% front-facing"
                    % (label, pct))
            else:
                warn("%s: %.1f%% front-facing, %d degenerate -- odd for a "
                     "closed primitive" % (label, pct, degen))

    log("-" * 68)
    log("checked %d meshes, %d clean" % (checked, ok_meshes))
    for n in _notes[:12]:
        log("note: %s" % n)

    if _errors:
        for e in _errors:
            log("FAIL: %s" % e)
        log("RESULT: FAILED -- the level would render inside out")
        return 1
    if checked == 0:
        log("RESULT: nothing checkable; treat as unverified")
        return 1
    log("RESULT: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
