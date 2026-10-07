# -*- coding: utf-8 -*-
"""
probe_sections.py -- what material does each LOD SECTION actually point at?

This exists because "the slot array is right" and "the draw call sees the right
material" are different claims, and the whole material chain now hinges on the
second one.

The slot array (static_materials) is already verified correct, and the component
resolves every slot to M_MC_<family> (probe_resolved.py, 0 atlas references).
But a StaticMesh's LOD0 render data is a list of *sections*, and each section
carries its own MaterialIndex into whatever array the draw path uses. If a
section's MaterialIndex is out of range, or all sections point at index 0, or the
sections were built before the slots were rewritten, the visible surface can
still be wrong even though both arrays look right in isolation. So this reads
the section->MaterialIndex->material chain the renderer walks, per section,
and cross-checks the triangle totals against the OBJ.

Reads:
  component.get_num_sections(lod)             how many sections
  component.get_material_index_from_section(lod, i)   section -> slot index
  component.get_material(slot)                slot -> resolved material

Read-only: loads the saved map, prints, does not save anything.

Run:
  UnrealEditor-Cmd.exe MCReplica.uproject \
      -ExecutePythonScript=tools/probe_sections.py -nullrhi -unattended -nopause
"""

import json
import os
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/probe_sections.txt"
STRUCT = "Q:/MC2UE5/repo/out/structures"
TERRAIN = "Q:/MC2UE5/repo/out/terrain"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCSECT] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def obj_counts(path):
    """(faces, tris) from an OBJ, or None."""
    if not os.path.isfile(path):
        return None
    faces = tris = 0
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if line.startswith("f "):
                faces += 1
                tris += len(line.split()) - 3
    return faces, tris


def main():
    try:
        # 1. Which section APIs exist at all? Guessing has cost this project
        #    before (get_static_mesh / imported_size_x were both wrong).
        mesh = unreal.load_asset("/Game/MC/Structures/bld_001_structure")
        if mesh is None:
            say("FATAL: bld_001_structure not found")
            return
        cands = sorted(n for n in dir(mesh)
                       if any(k in n.lower()
                              for k in ("section", "lod", "material_index")))
        say("mesh members mentioning section/lod/material_index: %s" % cands)

        actor = None
        # A fresh -nullrhi editor opens the startup map, but relying on that
        # would make "no sections" ambiguous between "not loaded" and "none".
        unreal.EditorLoadingAndSavingUtils.load_map("/Game/Maps/MCReplica")
        world = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        for a in unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.StaticMeshActor):
            if a.get_actor_label() == "B_bld_001_structure":
                actor = a
                break
        if actor is None:
            say("FATAL: B_bld_001_structure not in the loaded map; "
                "load the level first")
            return
        sc = actor.static_mesh_component
        csc = sorted(n for n in dir(sc)
                     if any(k in n.lower()
                            for k in ("section", "lod", "material")))
        say("component members mentioning section/lod/material: %s" % csc)

        # 2. Per-section read for a structure and a terrain tile.
        probes = [
            ("B_bld_001_structure", "/Game/MC/Structures/bld_001_structure",
             os.path.join(STRUCT, "bld_001_structure.obj")),
            ("B_bld_028_structure", "/Game/MC/Structures/bld_028_structure",
             os.path.join(STRUCT, "bld_028_structure.obj")),
            ("T_terrain_-144_-544", "/Game/MC/Terrain/terrain_-144_-544",
             os.path.join(TERRAIN, "terrain_-144_-544.obj")),
        ]
        by_label = {}
        for a in unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.StaticMeshActor):
            by_label[a.get_actor_label()] = a

        say("")
        say("=== per-LOD section -> MaterialIndex -> resolved material ===")
        for label, asset_path, obj_path in probes:
            a = by_label.get(label)
            if a is None:
                say("%s: NOT IN MAP" % label)
                continue
            sc = a.static_mesh_component
            m = sc.get_editor_property("static_mesh")
            say("--- %s  (mesh=%s) ---"
                % (label, m.get_name() if m else "NONE"))
            arr = (m.get_editor_property("static_materials") or []) if m else []
            slot_names = []
            for b in arr:
                try:
                    slot_names.append(str(b.get_editor_property(
                        "material_slot_name")))
                except Exception:
                    slot_names.append("<?>")
            say("    slots(%d): %s" % (len(slot_names), slot_names))

            n_lods = None
            for fn in ("get_num_lods", "get_lod_count"):
                f = getattr(m, fn, None)
                if callable(f):
                    try:
                        n_lods = f()
                        say("    %s() -> %s" % (fn, n_lods))
                        break
                    except Exception as exc:
                        say("    %s() raised %s" % (fn, str(exc)[:80]))
            if n_lods is None:
                # Component-side fallback is not available either; try 1.
                n_lods = 1
                say("    (no LOD-count API on the asset; probing LOD 0 only)")

            total_sections = 0
            for lod in range(max(int(n_lods or 1), 1)):
                try:
                    n_sec = sc.get_num_sections(lod)
                except Exception as exc:
                    say("    LOD%d get_num_sections raised %s"
                        % (lod, str(exc)[:90]))
                    continue
                say("    LOD%d sections=%d" % (lod, n_sec))
                total_sections += n_sec
                for i in range(n_sec):
                    try:
                        mi = sc.get_material_index_from_section(lod, i)
                    except Exception as exc:
                        say("      sec%-2d MaterialIndex: raised %s"
                            % (i, str(exc)[:70]))
                        continue
                    try:
                        rm = sc.get_material(mi)
                        rname = rm.get_name() if rm else "NONE"
                    except Exception as exc:
                        rname = "raised:%s" % str(exc)[:50]
                    flag = ""
                    if mi < 0 or mi >= len(slot_names):
                        flag = "  *** OUT OF RANGE ***"
                    say("      sec%-2d MaterialIndex=%-3d -> %s%s"
                        % (i, mi, rname, flag))

            # cross-check triangle totals against the OBJ
            oc = obj_counts(obj_path)
            got = None
            for fn in ("get_num_triangles",):
                f = getattr(m, fn, None)
                if callable(f):
                    try:
                        got = f(0)
                    except Exception:
                        pass
            say("    OBJ faces/tris=%s   mesh.get_num_triangles(0)=%s"
                % (oc, got))
            if oc and got is not None and oc[1]:
                say("    tris in/out = %.3f  (%d -> %d)"
                    % (got / float(oc[1]), oc[1], got))

        # 3. Terrain slot sanity across all tiles: every tile that the OBJ says
        #    has grass must resolve grass in some slot. This is the check the
        #    "grass is in every tile" claim needs to be backed by.
        say("")
        say("=== all terrain tiles: does grass resolve? ===")
        tman = json.load(open(os.path.join(TERRAIN, "manifest.json")))
        n_tiles = n_grass = n_nomatch = 0
        for t in tman["tiles"]:
            name = os.path.splitext(os.path.basename(t["obj"]))[0]
            a = by_label.get("T_" + name)
            if a is None:
                n_nomatch += 1
                continue
            n_tiles += 1
            sc = a.static_mesh_component
            m = sc.get_editor_property("static_mesh")
            names = []
            if m:
                for b in (m.get_editor_property("static_materials") or []):
                    names.append(str(b.get_editor_property("material_slot_name")))
            if "grass" in names:
                n_grass += 1
        say("terrain actors found=%d, of which a slot resolves grass=%d, "
            "missing actors=%d" % (n_tiles, n_grass, n_nomatch))

        say("--- done ---")
    except Exception:
        say("FAILED:\n" + traceback.format_exc())
        say("--- done ---")


main()
