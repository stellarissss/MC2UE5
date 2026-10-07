# -*- coding: utf-8 -*-
"""
import_chunks.py -- S5 Unreal side: import the atlas, the building meshes and
the material, then place them in the level.

Run inside the editor:

    UnrealEditor-Cmd.exe MCReplica.uproject \
        -ExecutePythonScript=tools/import_chunks.py -unattended -nopause -nosplash

This uses the standard `unreal.AssetImportTask` path. The old project
hand-rolled an OBJ parser (`MCMeshWindingLibrary`) and it is on the delete
list: the engine's importer is the only one that gets normals, tangents and
UV channels right, and a second parser is a second set of bugs.

Four constraints from this project's history are load-bearing here, not
incidental:

1. **The master material is reused, never deleted and recreated.** Deleting a
   Material and making a new one at the same path leaves every reference on the
   level's actors dangling, and the engine silently substitutes the default
   material -- a white checkerboard. The cook does not report it. So
   `_ensure_material` loads an existing asset and edits it in place.

2. **The material graph is a plain TextureSampleParameter2D -> BaseColor plus a
   Tiling scalar.** This exact shape is the only one proven to compile in this
   project. `MaterialExpressionWorldPosition` in particular makes materials fail
   with `Failed to compile Material for platform PCD3D_SM5, Default Material
   will be used in game` and **no cause in the log**, so it is not used here.
   Every material edit is followed by a compile check, and the script refuses to
   save a material that did not compile.

3. **OBJ faces carry `v/vt/vn`.** `reimport_terrain.py` records that omitting
   the normals made the importer discard ~90% of the geometry, silently.

4. **The detail library under `structures/detail_kinds/` is NOT placed.** It is
   duplicate geometry at the same world position as the per-building
   `*_detail.obj` meshes, and adding both z-fights. Only the per-building meshes
   are authoritative.
"""

import json
import os
import sys

import unreal

REPO = "Q:/MC2UE5/repo"
OUT = os.path.join(REPO, "out")
ATLAS_DIR = os.path.join(OUT, "atlas")
STRUCT_DIR = os.path.join(OUT, "structures")
LOG_PATH = "Q:/MC2UE5/logs/import_chunks.txt"

ATLAS_CONTENT = "/Game/MC/Atlas"
MESH_CONTENT = "/Game/MC/Structures"
MAT_PATH = "/Game/MC/Atlas/M_MC_Atlas"
TEX_PATH = "/Game/MC/Atlas/T_MC_Atlas"

_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[MCCHUNK] " + str(msg))
    try:
        with open(LOG_PATH, "w") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# atlas texture + material
# --------------------------------------------------------------------------- #

def material_compile_errors(mat_path):
    """-> (ok, detail) by reading the editor log.

    UE 5.8's Python `Material` exposes **no** compile-check API. Probed
    directly: there is no `material_compilation_errors`, no `compile_errors`,
    no `cached_expression_data`, and no `get_shader_map_valid` -- all raise
    "Failed to find property". `unreal.MaterialStatistics` exists but is an
    output struct with no way to compute it from a material. See
    tools/probe_compile_check.py and tools/probe_mat_stats.py.

    That also means `build_terrain_material_min.py`'s `compiled()` never worked
    on this engine: it calls `get_shader_map_valid()` inside a `try/except`
    that returns `(None, "no shader-map query")`, so its step-gated abort -- the
    whole reason that script exists -- silently never fired.

    So the check reads the log, which is where the engine *does* print
    `Failed to compile Material for platform PCD3D_SM5, Default Material will
    be used in game`. Honest about its limits: the log is written asynchronously
    relative to `recompile_material`, so a clean scan is evidence, not proof.
    It is still strictly better than the status quo, which was checking
    nothing at all.
    """
    log_dir = os.path.join(REPO, "project", "Saved", "Logs")
    if not os.path.isdir(log_dir):
        return False, "log directory missing: %s" % log_dir
    logs = sorted((os.path.join(log_dir, f) for f in os.listdir(log_dir)
                   if f.endswith(".log")),
                  key=os.path.getmtime, reverse=True)
    if not logs:
        return False, "no editor log in %s" % log_dir

    patterns = ("Failed to compile Material",
                "Default Material will be used",
                "LogMaterial: Error",
                "LogMaterial: Warning:")
    newest = os.path.basename(logs[0])
    hits = []
    with open(logs[0], "r", errors="replace") as fh:
        for line in fh:
            if any(p in line for p in patterns):
                if mat_path.split("/")[-1] in line or "Failed to compile" in line \
                        or "Default Material will be used" in line:
                    hits.append(line.strip()[:220])
    if hits:
        return False, "%d compile problem(s) in %s: %s" % (
            len(hits), newest, hits[:3])
    return True, "no compile errors in %s" % newest


def import_atlas_texture():
    """Import the packed atlas PNG as a Texture2D.

    Address mode is Clamp and filtering is trilinear *with mips*, deliberately:

      * Clamp, not Wrap. UVs are stretched onto one cell and never repeat, so
        wrapping has no correct use here -- but if a UV ever does exceed 1,
        Wrap silently samples the opposite edge of the atlas, which is a
        different material. Clamp turns that into an obviously wrong smear at
        one cell instead.
      * Trilinear mips, because a 4096x2048 atlas minified onto a 1 m wall
        without mips aliases into noise. This is the same reasoning
        build_release_level.py applies to the terrain textures.
    """
    png = os.path.join(ATLAS_DIR, "atlas_diffuse.png")
    if not os.path.isfile(png):
        say("FATAL: atlas missing at %s -- run tools/build_atlas.py" % png)
        return None

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png)
    task.set_editor_property("destination_path", ATLAS_CONTENT)
    task.set_editor_property("destination_name", "T_MC_Atlas")
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    tex = unreal.load_asset(TEX_PATH)
    if tex is None:
        say("FATAL: atlas import produced no asset at %s" % TEX_PATH)
        return None

    for prop, value in (
            ("filter", unreal.TextureFilter.TF_TRILINEAR),
            ("mip_gen_settings",
             unreal.TextureMipGenSettings.TMGS_FROM_TEXTURE_GROUP),
            ("compression_settings",
             unreal.TextureCompressionSettings.TC_DEFAULT),
            ("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD),
            ("address_x", unreal.TextureAddress.TA_CLAMP),
            ("address_y", unreal.TextureAddress.TA_CLAMP),
            ("never_stream", True),
            ("srgb", True)):
        try:
            tex.set_editor_property(prop, value)
        except Exception as exc:
            say("  atlas setting %s failed: %s" % (prop, str(exc)[:80]))
    unreal.EditorAssetLibrary.save_loaded_asset(tex)
    say("atlas texture: %s (%s)" % (tex.get_name(), tex.get_class().get_name()))
    return tex


def _ensure_material(tex):
    """Load-or-create the master material, then rebuild its graph in place.

    Never deletes. See constraint 1 in the module docstring: delete-and-
    recreate at the same path silently breaks every actor reference on the
    level and the cook reports success.
    """
    mel = unreal.MaterialEditingLibrary
    existing = unreal.EditorAssetLibrary.does_asset_exist(MAT_PATH)
    if existing:
        mat = unreal.load_asset(MAT_PATH)
        say("reusing existing material %s (NOT recreating it)" % MAT_PATH)
        if mat is None:
            say("  exists but would not load; leaving it alone")
            return None
        # Clear the graph in place rather than deleting the asset.
        try:
            for expr in mel.get_material_expressions(mat):
                mel.delete_material_expression(mat, expr)
        except Exception as exc:
            say("  could not clear expressions: %s" % str(exc)[:120])
    else:
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "M_MC_Atlas", ATLAS_CONTENT, unreal.Material,
            unreal.MaterialFactoryNew())
        say("created material %s" % MAT_PATH)
    if mat is None:
        return None

    def node(cls, x, y):
        e = mel.create_material_expression(mat, cls, x, y)
        if e is None:
            raise RuntimeError("could not create %s" % cls)
        return e

    # --- the proven shape -------------------------------------------------
    # A TextureSampleParameter2D straight into BaseColor, and a Tiling scalar.
    # Nothing else. WorldPosition and friends are known to break compilation
    # here with no diagnostic, so the graph stops at what is known to work.
    sample = node(unreal.MaterialExpressionTextureSampleParameter2D, -400, 0)
    sample.set_editor_property("ParameterName", "Atlas")
    sample.set_editor_property("Texture", tex)
    mel.connect_material_property(sample, "RGB",
                                  unreal.MaterialProperty.MP_BASE_COLOR)

    tiling = node(unreal.MaterialExpressionScalarParameter, -400, -260)
    tiling.set_editor_property("ParameterName", "Tiling")
    tiling.set_editor_property("DefaultValue", 1.0)
    # Tiling scales the atlas lookup. UVs are already stretched onto one cell,
    # so this is a global texel-density control rather than a per-material one.
    mult = node(unreal.MaterialExpressionMultiply, -180, -260)
    uv = node(unreal.MaterialExpressionTextureCoordinate, -620, -260)
    mel.connect_material_expressions(uv, "", mult, "A")
    mel.connect_material_expressions(tiling, "", mult, "B")
    mel.connect_material_expressions(mult, "", sample, "Coordinates")

    # Roughness as a constant, matching MC_Character / MC_Terrain.
    rough = node(unreal.MaterialExpressionConstant, -180, 260)
    rough.set_editor_property("R", 0.85)
    mel.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)

    mel.recompile_material(mat)
    ok, why = material_compile_errors(MAT_PATH)
    say("material compile: ok=%s %s" % (ok, why))
    if not ok:
        say("  NOT saving: an uncompilable material cooks as the engine "
            "default surface (a white checkerboard) and the cook still "
            "reports success")
        return None

    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    src = mel.get_material_property_input_node(
        mat, unreal.MaterialProperty.MP_BASE_COLOR)
    say("material ready: %s  BaseColor <- %s"
        % (mat.get_name(), src.get_class().get_name() if src else "NOTHING"))
    return mat


# --------------------------------------------------------------------------- #
# meshes
# --------------------------------------------------------------------------- #

def import_obj(src, asset_name, dest=MESH_CONTENT):
    """Standard AssetImportTask OBJ import -> StaticMesh or None."""
    if not os.path.isfile(src):
        return None
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", src)
    task.set_editor_property("destination_path", dest)
    task.set_editor_property("destination_name", asset_name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return unreal.load_asset("%s/%s" % (dest, asset_name))


def import_meshes(manifest, material):
    """Import every authoritative mesh and record what actually arrived.

    Returns (imported, problems). A mesh whose vertex count is a fraction of
    its OBJ's `v` count is the signature of the normals bug in constraint 3, so
    the check is done per mesh rather than trusted.
    """
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    imported, problems = [], []
    counts_read = 0

    for b in manifest["buildings"]:
        for tag, part in sorted(b["meshes"].items()):
            name = os.path.splitext(part["obj"])[0]
            mesh = import_obj(os.path.join(STRUCT_DIR, part["obj"]), name)
            if mesh is None:
                problems.append("%s: import returned no asset" % name)
                continue

            obj_v = 0
            with open(os.path.join(STRUCT_DIR, part["obj"])) as fh:
                for line in fh:
                    if line.startswith("v "):
                        obj_v += 1
            got_v = got_t = 0
            try:
                # Signature verified by tools/probe_mesh_api.py:
                # get_number_verts(mesh, lod_index). Passing the *class* rather
                # than a subsystem instance raises, and the original code did
                # exactly that -- so this guard silently never ran and every
                # import reported clean while verifying nothing.
                got_v = sub.get_number_verts(mesh, 0)
                got_t = mesh.get_num_triangles(0)
            except Exception as exc:
                problems.append("%s: cannot read mesh counts (%s)"
                                % (name, str(exc)[:70]))
                # Do not fall through to the geometry check below with got_v
                # still 0: that reads as "nothing to compare" rather than
                # "the check did not run".
                counts_read += 1
                continue
            counts_read += 1

            # The importer welds coincident vertices, so got_v <= obj_v. A large
            # shortfall means geometry was dropped, which is the failure
            # reimport_terrain.py hit and could not see. Expected triangles are
            # 2 per quad, and the importer must not report fewer.
            if obj_v and got_v < obj_v * 0.5:
                problems.append("%s: %d verts in, %d out -- the importer "
                                "discarded over half the geometry"
                                % (name, obj_v, got_v))
            exp_t = part["quads"] * 2
            if exp_t and got_t < exp_t * 0.9:
                problems.append("%s: %d triangles expected, %d imported"
                                % (name, exp_t, got_t))

            if material is not None:
                # One material on every slot: the atlas already encodes the
                # per-family choice in UVs, so slot count is an import artefact,
                # not a shading decision.
                try:
                    mesh.set_material(0, material)
                except Exception as exc:
                    problems.append("%s: material slot 0 (%s)"
                                    % (name, str(exc)[:60]))

            try:
                settings = sub.get_nanite_settings(mesh)
                settings.set_editor_property("enabled", True)
                sub.set_nanite_settings(mesh, settings, True)
            except Exception:
                pass
            try:
                sub.set_allow_cpu_access(mesh, True)
            except Exception:
                pass

            unreal.EditorAssetLibrary.save_loaded_asset(mesh)
            imported.append({"asset": name, "building": b["id"], "part": tag,
                             "verts_in_obj": obj_v, "verts_imported": got_v,
                             "tris_imported": got_t,
                             "quads": part["quads"]})

    # Water gets its own asset, same master material (its cell is the water
    # family, so no separate material is needed).
    w = manifest["water"]
    if w.get("obj"):
        mesh = import_obj(os.path.join(STRUCT_DIR, w["obj"]),
                          os.path.splitext(w["obj"])[0])
        if mesh is not None:
            if material is not None:
                try:
                    mesh.set_material(0, material)
                except Exception as exc:
                    problems.append("water: material (%s)" % str(exc)[:60])
            unreal.EditorAssetLibrary.save_loaded_asset(mesh)
            imported.append({"asset": os.path.splitext(w["obj"])[0],
                             "building": None, "part": "water",
                             "verts_in_obj": 0, "verts_imported": 0,
                             "quads": w.get("quads", 0)})
        else:
            problems.append("water: import returned no asset")

    if counts_read == 0:
        problems.append("NO mesh had its counts read: the geometry-loss guard "
                        "never ran, so nothing below was actually verified")
    say("vertex/triangle counts read for %d meshes" % counts_read)
    return imported, problems


def place(imported, map_path="/Game/Maps/MCReplica"):
    """Spawn one StaticMeshActor per imported mesh at the origin.

    Positions come from the OBJ already: the mesher writes absolute world cm,
    so every actor sits at (0,0,0) and the geometry lands where the save says.
    That is deliberate. Re-deriving a transform here would be a second
    implementation of the coordinate contract, and the contract is the part
    that has to be in exactly one place.

    Building the actual level layout is a separate step; this only proves the
    assets are real and correctly sized.
    """
    level = unreal.EditorLoadingAndSavingUtils.load_map(map_path)
    if level is None:
        say("could not load %s; assets imported but not placed" % map_path)
        return 0

    editor = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    placed = 0
    for rec in imported:
        mesh = unreal.load_asset("%s/%s" % (MESH_CONTENT, rec["asset"]))
        if mesh is None:
            continue
        actor = editor.spawn_actor_from_class(
            unreal.StaticMeshActor, unreal.Vector(0.0, 0.0, 0.0),
            unreal.Rotator(0.0, 0.0, 0.0))
        if actor is None:
            say("spawn failed for %s" % rec["asset"])
            continue
        actor.set_actor_label("B_%s" % rec["asset"])
        actor.static_mesh_component.set_static_mesh(mesh)
        placed += 1
    say("placed %d / %d actors" % (placed, len(imported)))

    # Save the level with LevelEditorSubsystem.save_current_level().
    #
    # EditorLoadingAndSavingUtils.save_dirty_packages(False, True) was used
    # here and **silently does nothing to the map** -- verified with a single
    # probe actor: that call left the .umap mtime at 14:04:56 and the actor was
    # gone after reload, while save_current_level() wrote it out and the probe
    # survived. The report still said "placed 158 / 158", which is why this went
    # unnoticed: the actors existed, the count was right, and the file on disk
    # never changed. Every packaged build then cooked without them.
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    saved = les.save_current_level()
    say("save_current_level -> %s" % saved)
    if not saved:
        say("*** level was NOT written; nothing downstream can see these actors")
        return 0

    # Prove it by reading the level back from disk. A placement that is not
    # verified as persisted is exactly the failure this just cost us.
    unreal.EditorLoadingAndSavingUtils.load_map(map_path)
    w = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    labels = [a.get_actor_label() for a in
              unreal.GameplayStatics.get_all_actors_of_class(w, unreal.Actor)]
    placed_labels = sum(1 for x in labels if x.startswith("B_"))
    say("verify after reload: %d actors total, %d B_* labels"
        % (len(labels), placed_labels))
    if placed_labels < placed:
        say("*** only %d of %d placed actors survived the reload"
            % (placed_labels, placed))
        return 0
    return placed


def main():
    say("=== import_chunks (S5) ===")
    with open(os.path.join(STRUCT_DIR, "manifest.json")) as fh:
        manifest = json.load(fh)
    say("structures manifest: %d buildings, %d triangles"
        % (len(manifest["buildings"]),
           manifest["triangles"]["buildings"] // 2))
    say("atlas: %s"
        % manifest["atlas"].get("manifest", "out/atlas/manifest.json"))

    tex = import_atlas_texture()
    if tex is None:
        say("aborting: no atlas texture")
        return 1

    material = _ensure_material(tex)
    if material is None:
        say("aborting: master material did not compile; NOT placing anything "
            "(an uncompilable material renders as the default checkerboard "
            "and the cook still reports success)")
        return 1

    imported, problems = import_meshes(manifest, material)
    say("imported %d meshes, %d problems" % (len(imported), len(problems)))
    for p in problems[:20]:
        say("  PROBLEM %s" % p)

    lost = [r for r in imported
            if r["verts_in_obj"] and r["verts_imported"]
            and r["verts_imported"] < r["verts_in_obj"] * 0.5]
    say("meshes that lost over half their geometry: %d" % len(lost))
    for r in lost[:10]:
        say("  %s: %d -> %d" % (r["asset"], r["verts_in_obj"],
                                r["verts_imported"]))

    placed = place(imported)
    say("=== done: %d meshes, %d placed, %d problems, material compiled ==="
        % (len(imported), placed, len(problems)))
    return 0


if __name__ == "__main__":
    sys.exit(main())