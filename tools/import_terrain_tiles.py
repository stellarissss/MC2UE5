# -*- coding: utf-8 -*-
"""
import_terrain_tiles.py -- import and place the 24 smooth terrain tiles.

The structures landed (runtime diag reports meshes=158) but the terrain did not:
`terrain(vis=0 col=0)` because no tile was ever imported. The flat brown ground
in the captures is still the top of the voxel layer.

Same save trap as the structures, so it is guarded here from the start:
EditorLoadingAndSavingUtils.save_dirty_packages does NOT persist the map, and a
placement that is not verified as persisted is the failure that cost a full
cycle last time.
"""
import json
import os
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/import_terrain_tiles.txt"
TERRAIN_SRC = "Q:/MC2UE5/repo/out/terrain"
DEST = "/Game/MC/Terrain"
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCTER] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def main():
    try:
        mf = os.path.join(TERRAIN_SRC, "manifest.json")
        with open(mf) as fh:
            man = json.load(fh)
        tiles = man["tiles"]
        say("manifest: %d tiles, %s triangles"
            % (len(tiles), format(man["triangles"], ",")))

        if not unreal.EditorAssetLibrary.does_directory_exist(DEST):
            unreal.EditorAssetLibrary.make_directory(DEST)

        assets = []
        for t in tiles:
            src = os.path.join(TERRAIN_SRC, os.path.basename(t["obj"]))
            if not os.path.exists(src):
                say("missing source: %s" % src)
                continue
            task = unreal.AssetImportTask()
            task.filename = src.replace("\\", "/")
            task.destination_path = DEST
            task.automated = True
            task.replace_existing = True
            task.save = True
            unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
            name = os.path.splitext(os.path.basename(src))[0]
            assets.append(name)
        say("imported %d tile assets" % len(assets))

        lvl = unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        say("load_map -> %s" % (lvl is not None))
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        placed = 0
        for name in assets:
            mesh = unreal.load_asset("%s/%s" % (DEST, name))
            if mesh is None:
                continue
            # Tiles carry absolute world cm, so the actor transform is identity --
            # the same contract extract_structures uses.
            a = eas.spawn_actor_from_class(
                unreal.StaticMeshActor, unreal.Vector(0, 0, 0),
                unreal.Rotator(0, 0, 0))
            if a is None:
                continue
            a.set_actor_label("T_%s" % name)
            a.static_mesh_component.set_static_mesh(mesh)
            placed += 1
        say("placed %d / %d tiles" % (placed, len(assets)))

        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        say("save_current_level -> %s" % les.save_current_level())

        unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        w = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        labels = [a.get_actor_label() for a in
                  unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor)]
        n_b = sum(1 for x in labels if x.startswith("B_"))
        n_t = sum(1 for x in labels if x.startswith("T_"))
        say("verify after reload: B_*=%d  T_*=%d  total=%d" % (n_b, n_t, len(labels)))
    except Exception:
        say("FAILED:/n" + traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
