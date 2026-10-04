# -*- coding: utf-8 -*-
"""
audit_level.py -- report what the packaged level actually contains.

Written after a review render came back with zero terrain meshes and zero props
in a level the build script had reported as fully populated. That gap matters:
World Partition stores actors outside the .umap, so ``get_all_level_actors``
only sees them once the relevant cells are streamed in, and a headless editor
session with no player never triggers that.

This separates the two questions that were being conflated:

  * what is **on disk** (the saved external actors) -- read straight off the
    ``__ExternalActors__`` directory;
  * what is **loaded in this session** (streamed actors).

Run inside the editor, or headless with -nullrhi.

    UnrealEditor.exe MCReplica.uproject \
        -ExecutePythonScript=project/Content/Python/audit_level.py \
        -unattended -nopause -nosplash -nullrhi
"""

import json
import os
import sys
import time

import unreal

_t0 = time.time()
_errors = []


def log(msg):
    unreal.log("[AUDIT %6.1fs] %s" % (time.time() - _t0, msg))


def err(msg):
    _errors.append(msg)
    unreal.log_error("[AUDIT] %s" % msg)


def project_dir():
    return unreal.Paths.convert_relative_path_to_full(
        unreal.Paths.project_dir()).rstrip("/\\")


def scan_external_actors():
    """
    -> (counts, files, dir) for the on-disk OFPA directory.

    UE 5.8 writes external actors as ``.uasset``, not the ``.uactor`` older
    versions used, and the filename is a package hash rather than the actor
    label -- so the label has to be read out of the file's name table. Getting
    the extension wrong here reports an empty level that is in fact fully
    populated, which is worse than no report at all.
    """
    base = os.path.join(project_dir(), "Content", "__ExternalActors__")
    out = {}
    files = 0
    if not os.path.isdir(base):
        return out, files, base

    for dirpath, _dirs, names in os.walk(base):
        for n in names:
            if not n.endswith((".uasset", ".uactor")):
                continue
            files += 1
            path = os.path.join(dirpath, n)
            try:
                with open(path, "rb") as fh:
                    head = fh.read(8192)
            except OSError:
                continue
            label = _label_from_package(head)
            if not label:
                out["<unlabelled>"] = out.get("<unlabelled>", 0) + 1
                continue
            key = label.split("_")[0] if "_" in label else label
            out[key] = out.get(key, 0) + 1
    return out, files, base


def _label_from_package(blob):
    """Pull the actor label out of a .uactor's name table."""
    try:
        text = blob.decode("utf-16-le", errors="ignore")
    except Exception:
        return None
    for tag in ("TerrainCollision_", "Terrain_", "Prop_", "MC_"):
        idx = text.find(tag)
        if idx >= 0:
            tail = text[idx:idx + 64]
            return "".join(ch for ch in tail
                           if ch.isalnum() or ch in "_-")
    return None


def count_loaded():
    """-> (labels, total) for actors currently loaded in the session."""
    labels = {}
    total = 0
    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        total += 1
        label = actor.get_actor_label()
        key = label.split("_")[0] if "_" in label else label
        labels[key] = labels.get(key, 0) + 1
    return labels, total


def run():
    log("=" * 68)
    log("MC2UE5 level audit")
    log("=" * 68)

    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        err("no editor world")
        return 1

    log("map: %s" % world.get_name())

    loaded, total = count_loaded()
    log("")
    log("LOADED IN THIS SESSION: %d actors" % total)
    for k in sorted(loaded):
        log("  %-24s %d" % (k, loaded[k]))
    if not loaded:
        log("  (nothing -- the persistent level holds only World Partition "
            "helpers; the content lives in external actors)")

    log("")
    log("ON DISK (__ExternalActors__):")
    counts, files, _ = scan_external_actors()
    log("  %d .uactor files" % files)
    for k in sorted(counts):
        log("  %-24s %d" % (k, counts[k]))

    # World Partition only loads the cells around a viewer. With no local
    # player in a -nullrhi session, that set can legitimately be empty, which
    # is why the packaged build's own actor count has to come from the cook,
    # not from this session.
    log("")
    try:
        wpc = world.get_world_partition()
        log("WorldPartition present: %s" % (wpc is not None))
    except Exception as exc:
        log("WorldPartition: not reachable (%s)" % str(exc)[:80])

    log("")
    log("-" * 68)
    if _errors:
        for e in _errors:
            log("error: %s" % e)
        return 1
    log("AUDIT OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
