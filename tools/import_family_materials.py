# -*- coding: utf-8 -*-
"""
import_family_materials.py -- one tiling texture + one material per family, and
(optionally) rebind every mesh slot to its family's material.

Why this exists (read before touching it):

The atlas model is wrong for this project. The mesher stretches each merged quad
onto its one atlas cell ("UV convention: each merged quad is stretched onto its
material's atlas cell, never tiled across it"), so a 1 m block shows the whole
504 px cell and the *average* of the cell is what lands on screen -- a flat
colour per block. That is the "the whole campus is one tone" symptom, and no
amount of atlas baking, tinting or lighting fixes it, because the addressing is
correct and the result is a voxel-coloured surface.

The fix is the standard one for architecture: a **per-family tiling texture**
addressed with **Wrap**, sampled through a **per-family material**, over meshes
whose UVs are in **block units** (1 block = 1 repeat) instead of "whole cell".
`tools/extract_structures.py --uv-mode block` produces those meshes.

This script is split into two stages on purpose, because the texture/material
step is the high-risk one and is worth verifying alone:

    --stage materials   import T_MC_<family> (Wrap) + build M_MC_<family>
    --stage meshes      reimport block-UV meshes + bind slots + save level + shot
    --stage all         both, in order (default)
    --stage verify      read-only audit of what is on disk (no writes)

Run inside the editor:

    Q:\\UE\\UE_5.8\\Engine\\Binaries\\Win64\\UnrealEditor-Cmd.exe \
        Q:\\MC2UE5\\repo\\project\\MCReplica.uproject \
        -ExecutePythonScript=Q:\\MC2UE5\\repo\\tools\\import_family_materials.py \
        --stage=materials -unattended -nopause -nosplash

Log: Q:/MC2UE5/logs/import_family_materials.txt (flushed line by line, ends with
`RESULT: PASS/FAIL` and `--- done ---`).

Four constraints carried over from this project's history -- all load-bearing:

1. **Wrap, not Clamp.** The atlas texture is Clamp and that is correct *for the
   atlas* (UVs never repeat). These family textures are the opposite: they are
   sampled with block-unit UVs that exceed 1 on every quad wider than one block,
   so Clamp would smear the edge texel across the whole surface. If address_x/y
   ends up TA_CLAMP there is no tiling at all and this whole change is silently
   defeated -- which is exactly why the read-back below prints the real values.

2. **The material graph is the one proven shape, nothing else.** A
   TextureSampleParameter2D -> BaseColor, a Tiling scalar multiplied into
   TextureCoordinate -> sample.Coordinates, and a Constant -> Roughness.
   `MaterialExpressionWorldPosition` makes materials fail in this project with
   `Failed to compile Material ... Default Material will be used in game` and no
   cause in the log, so it is not used. Every edit is followed by a compile
   check and a material that fails the check is **not saved** -- an uncompilable
   material renders as the engine checkerboard and the cook still says OK.

3. **A material is reused, never deleted and recreated.** Delete-and-recreate at
   the same path leaves every actor reference dangling; the engine substitutes
   the default material, and the cook reports success. So an existing material
   is loaded and its expressions cleared in place (`_ensure_material` in
   tools/import_chunks.py is the model).

4. **Mesh slots are written to the ASSET, and slot order == usemtl first-seen
   order.** Component-level `set_material` was proven to do nothing on screen in
   this project, and `StaticMesh` has no `set_material`/`get_num_materials` in
   UE 5.8 Python -- the slots live in the `static_materials` UPROPERTY array.
   The OBJ writes `usemtl <family>` in first-seen order and the importer turns
   each group into a slot in that order; the code asserts the two lengths match
   and refuses the mesh otherwise rather than guessing.
"""

import json
import os
import sys
import time
import traceback

import unreal

REPO = "Q:/MC2UE5/repo"
OUT = os.path.join(REPO, "out")
FAMILY_DIR = os.path.join(OUT, "families")
STRUCT_DIR = os.path.join(OUT, "structures")
TERRAIN_DIR = os.path.join(OUT, "terrain")
LOG_PATH = "Q:/MC2UE5/logs/import_family_materials.txt"

FAMILY_CONTENT = "/Game/MC/Families"
STRUCT_CONTENT = "/Game/MC/Structures"
TERRAIN_CONTENT = "/Game/MC/Terrain"
MAP_PATH = "/Game/Maps/MCReplica"

TEX_FMT = FAMILY_CONTENT + "/T_MC_%s"
MAT_FMT = FAMILY_CONTENT + "/M_MC_%s"
MI_FMT = FAMILY_CONTENT + "/MI_MC_%s"

# Same list and order as build_atlas.ATLAS_FAMILIES. Kept as a literal because
# importing build_atlas here would pull numpy/PIL into the editor's embedded
# interpreter; the order is asserted against the families manifest at runtime.
FAMILIES = [
    "grass", "path", "soil", "asphalt", "concrete", "plaster", "brick",
    "granite", "tiles", "roof", "wood", "bark", "leaves", "metal", "gravel",
    "rock", "fabric", "quartz", "greystone", "other", "water", "sports",
]

_LINES = []


def say(msg):
    """Append one line to the in-memory log and flush it to disk.

    Flushed every line, like the other tools in this folder: when the editor
    crashes mid-run the tail is the only evidence of where it got to.
    """
    _LINES.append(str(msg))
    unreal.log("[MCFAM] " + str(msg))
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_LINES) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# compile checking (the only thing that can see an uncompilable material)
# --------------------------------------------------------------------------- #

def newest_editor_log():
    log_dir = os.path.join(REPO, "project", "Saved", "Logs")
    if not os.path.isdir(log_dir):
        return None
    logs = [os.path.join(log_dir, f) for f in os.listdir(log_dir)
            if f.endswith(".log")]
    if not logs:
        return None
    # Prefer the live project log; fall back to newest by mtime.
    live = os.path.join(log_dir, "MCReplica.log")
    if os.path.isfile(live):
        return live
    return max(logs, key=os.path.getmtime)


def current_log_offset():
    log = newest_editor_log()
    try:
        return os.path.getsize(log) if log else 0
    except OSError:
        return 0


def scan_compile_errors(mat_name, offset, tries=6, gap=0.2):
    """Scan only the editor-log region written **after** `offset`.

    UE 5.8's Python `Material` exposes no compile-check API (probed: no
    `material_compilation_errors`, no `cached_expression_data`, no
    `get_shader_map_valid`), so the log is the only witness, exactly as
    `tools/import_chunks.py::material_compile_errors` documents. Unlike that
    version this scans from a byte offset captured just before
    `recompile_material`, so a compile error left by an *earlier* material in
    the same run cannot be mis-attributed to this one.

    The engine writes the log asynchronously relative to the compile call, so a
    clean scan is polled a few times before it is believed. A clean result is
    evidence, not proof; the read-back pass after this is the stronger check.
    """
    log = newest_editor_log()
    if log is None:
        return False, "no editor log found"
    try:
        size = os.path.getsize(log)
    except OSError:
        return False, "cannot stat editor log"
    if offset > size:
        offset = 0  # log rotated mid-run

    hits = []
    pos = offset
    for attempt in range(tries):
        try:
            with open(log, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
                pos = fh.tell()
        except OSError as exc:
            return False, "cannot read editor log: %s" % exc
        for line in data.decode("utf-8", "replace").splitlines():
            if ("Failed to compile Material" in line
                    or "Default Material will be used" in line
                    or "LogMaterial: Error" in line):
                if (mat_name in line
                        or "Failed to compile" in line
                        or "Default Material will be used" in line):
                    hits.append(line.strip()[:220])
        if hits:
            break
        if attempt < tries - 1:
            time.sleep(gap)
    if hits:
        return False, "%d compile problem(s): %s" % (len(hits), hits[:3])
    return True, "clean (%s)" % os.path.basename(log)


# --------------------------------------------------------------------------- #
# stage = materials
# --------------------------------------------------------------------------- #

def check_family_list():
    """The literal FAMILIES must equal the families manifest, or the textures
    and the meshes' `usemtl` names would disagree."""
    mf = os.path.join(FAMILY_DIR, "manifest.json")
    if not os.path.isfile(mf):
        say("FATAL: families manifest missing at %s" % mf)
        return False
    with open(mf, encoding="utf-8") as fh:
        man = json.load(fh)
    listed = [f["family"] for f in man["families"]]
    if listed != FAMILIES:
        say("FATAL: family list mismatch\n  code : %s\n  disk : %s"
            % (FAMILIES, listed))
        return False
    say("family list matches manifest: %d families" % len(FAMILIES))
    return True


def import_texture(family):
    """Import out/families/<family>.png -> /Game/MC/Families/T_MC_<family>."""
    png = os.path.join(FAMILY_DIR, family + ".png")
    if not os.path.isfile(png):
        return None, "png missing: %s" % png

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png)
    task.set_editor_property("destination_path", FAMILY_CONTENT)
    task.set_editor_property("destination_name", "T_MC_" + family)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    tex = unreal.load_asset(TEX_FMT % family)
    if tex is None:
        return None, "import produced no asset at %s" % (TEX_FMT % family)

    # Wrap is the whole point. Clamp here = no tiling = the exact bug this
    # change removes.
    settings = (
        ("address_x", unreal.TextureAddress.TA_WRAP),
        ("address_y", unreal.TextureAddress.TA_WRAP),
        ("srgb", True),
        ("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD),
        ("never_stream", True),
        ("compression_settings",
         unreal.TextureCompressionSettings.TC_DEFAULT),
        ("mip_gen_settings",
         unreal.TextureMipGenSettings.TMGS_FROM_TEXTURE_GROUP),
    )
    for prop, value in settings:
        try:
            tex.set_editor_property(prop, value)
        except Exception as exc:
            return None, "set %s failed: %s" % (prop, str(exc)[:90])

    unreal.EditorAssetLibrary.save_loaded_asset(tex)

    # Read back what actually stuck. set_editor_property that silently does
    # nothing has been this project's failure mode more than once.
    ax = tex.get_editor_property("address_x")
    ay = tex.get_editor_property("address_y")
    if ax != unreal.TextureAddress.TA_WRAP or ay != unreal.TextureAddress.TA_WRAP:
        return None, "address_x/y read back as %s/%s (expected TA_WRAP)" % (ax, ay)
    return tex, "ok"


def build_material(family, tex):
    """Load-or-create /Game/MC/Families/M_MC_<family>, rebuild the proven graph."""
    mel = unreal.MaterialEditingLibrary
    mat_path = MAT_FMT % family

    if unreal.EditorAssetLibrary.does_asset_exist(mat_path):
        mat = unreal.load_asset(mat_path)
        if mat is None:
            return None, "exists but would not load (NOT recreating it)"
        try:
            for expr in mel.get_material_expressions(mat):
                mel.delete_material_expression(mat, expr)
        except Exception as exc:
            return None, "could not clear expressions: %s" % str(exc)[:120]
    else:
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "M_MC_%s" % family, FAMILY_CONTENT, unreal.Material,
            unreal.MaterialFactoryNew())
    if mat is None:
        return None, "create/load returned None"

    def node(cls, x, y):
        e = mel.create_material_expression(mat, cls, x, y)
        if e is None:
            raise RuntimeError("could not create %s" % cls.get_name())
        return e

    # --- the one proven shape. Nothing else. ------------------------------
    try:
        sample = node(unreal.MaterialExpressionTextureSampleParameter2D, -400, 0)
        sample.set_editor_property("ParameterName", family)
        sample.set_editor_property("Texture", tex)
        mel.connect_material_property(sample, "RGB",
                                      unreal.MaterialProperty.MP_BASE_COLOR)

        tiling = node(unreal.MaterialExpressionScalarParameter, -400, -260)
        tiling.set_editor_property("ParameterName", "Tiling")
        tiling.set_editor_property("DefaultValue", 1.0)

        uv = node(unreal.MaterialExpressionTextureCoordinate, -620, -260)
        mult = node(unreal.MaterialExpressionMultiply, -180, -260)
        mel.connect_material_expressions(uv, "", mult, "A")
        mel.connect_material_expressions(tiling, "", mult, "B")
        mel.connect_material_expressions(mult, "", sample, "Coordinates")

        rough = node(unreal.MaterialExpressionConstant, -180, 260)
        rough.set_editor_property("R", 0.85)
        mel.connect_material_property(rough, "",
                                      unreal.MaterialProperty.MP_ROUGHNESS)
    except Exception as exc:
        return None, "graph build failed: %s" % str(exc)[:150]

    offset = current_log_offset()
    mel.recompile_material(mat)
    ok, why = scan_compile_errors("M_MC_%s" % family, offset)
    if not ok:
        say("  NOT saving M_MC_%s: an uncompilable material cooks as the "
            "engine default (checkerboard) and the cook still reports success"
            % family)
        return None, "compile failed: %s" % why

    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    return mat, "ok"


def run_materials():
    """Import 22 textures, build 22 materials, verify each from a fresh load."""
    say("=== stage: materials ===")
    if not check_family_list():
        return False

    if not unreal.EditorAssetLibrary.does_directory_exist(FAMILY_CONTENT):
        unreal.EditorAssetLibrary.make_directory(FAMILY_CONTENT)

    say("--- importing %d textures (TA_WRAP) ---" % len(FAMILIES))
    textures = {}
    failed = []
    for fam in FAMILIES:
        tex, why = import_texture(fam)
        if tex is None:
            failed.append(fam)
            say("  %-11s TEXTURE FAIL  %s" % (fam, why))
        else:
            textures[fam] = tex
            say("  %-11s T_MC_%s  addr=%s/%s srgb=%s"
                % (fam, fam,
                   tex.get_editor_property("address_x").name,
                   tex.get_editor_property("address_y").name,
                   tex.get_editor_property("srgb")))
    say("textures ok: %d / %d" % (len(textures), len(FAMILIES)))

    say("--- building %d materials ---" % len(FAMILIES))
    built = {}
    for fam in FAMILIES:
        if fam not in textures:
            failed.append(fam)
            say("  %-11s MATERIAL SKIPPED (no texture)" % fam)
            continue
        mat, why = build_material(fam, textures[fam])
        if mat is None:
            failed.append(fam)
            say("  %-11s MATERIAL FAIL  %s" % (fam, why))
        else:
            built[fam] = mat
            say("  %-11s M_MC_%s ok" % (fam, fam))
    say("materials ok: %d / %d" % (len(built), len(FAMILIES)))

    # --- read back from a fresh load: the claim must come from disk --------
    say("--- verify (re-load every asset) ---")
    mel = unreal.MaterialEditingLibrary
    all_ok = True
    for fam in FAMILIES:
        problems = []
        tex = unreal.load_asset(TEX_FMT % fam)
        mat = unreal.load_asset(MAT_FMT % fam)
        if tex is None:
            problems.append("T_MC_%s missing" % fam)
        else:
            ax = tex.get_editor_property("address_x")
            ay = tex.get_editor_property("address_y")
            if ax != unreal.TextureAddress.TA_WRAP or ay != unreal.TextureAddress.TA_WRAP:
                problems.append("addr=%s/%s" % (ax.name, ay.name))
        bound = None
        base_class = None
        coord_class = "unverified"
        if mat is None:
            problems.append("M_MC_%s missing" % fam)
        else:
            # The proven graph is exactly these five nodes; any extra node (a
            # WorldPosition, say) is a change to a shape that is known to
            # compile, so the composition is asserted rather than assumed.
            expected = {
                "MaterialExpressionTextureSampleParameter2D": 1,
                "MaterialExpressionTextureCoordinate": 1,
                "MaterialExpressionScalarParameter": 1,
                "MaterialExpressionMultiply": 1,
                "MaterialExpressionConstant": 1,
            }
            counts = {}
            sample = None
            for e in mel.get_material_expressions(mat):
                c = e.get_class().get_name()
                counts[c] = counts.get(c, 0) + 1
                if c == "MaterialExpressionTextureSampleParameter2D":
                    sample = e
            if counts != expected:
                problems.append("graph=%s" % counts)
            if sample is None:
                problems.append("no TextureSampleParameter2D")
            else:
                pname = sample.get_editor_property("parameter_name")
                if pname != fam:
                    problems.append("sample param=%s" % pname)
                t = sample.get_editor_property("texture")
                bound = t.get_name() if t else None
                if bound != "T_MC_%s" % fam:
                    problems.append("sample bound to %s" % bound)
                # Only checked if the engine exposes the API; a missing API
                # reads as "unverified", never as a failure.
                fn = getattr(mel, "get_input_node", None)
                if fn is not None:
                    try:
                        cn = fn(sample, "Coordinates")
                        coord_class = cn.get_class().get_name() if cn else "NOTHING"
                    except Exception:
                        coord_class = "unverified"
                    if coord_class not in ("MaterialExpressionMultiply",
                                           "unverified"):
                        problems.append("Coordinates <- %s" % coord_class)
            base = mel.get_material_property_input_node(
                mat, unreal.MaterialProperty.MP_BASE_COLOR)
            base_class = base.get_class().get_name() if base else "NOTHING"
            if base_class != "MaterialExpressionTextureSampleParameter2D":
                problems.append("BaseColor <- %s" % base_class)

        if problems:
            all_ok = False
            say("  %-11s VERIFY FAIL  %s" % (fam, "; ".join(problems)))
        else:
            say("  %-11s OK  addr=%s/%s  sample<-%s  coords<-%s  BaseColor<-%s"
                % (fam,
                   tex.get_editor_property("address_x").name,
                   tex.get_editor_property("address_y").name,
                   bound, coord_class, base_class))

    if failed:
        say("failed families: %s" % sorted(set(failed)))
    say("stage materials: %d/%d textures, %d/%d materials"
        % (len(textures), len(FAMILIES), len(built), len(FAMILIES)))
    return all_ok and len(built) == len(FAMILIES)


# --------------------------------------------------------------------------- #
# stage = meshes
# --------------------------------------------------------------------------- #

def usemtl_first_seen_order(obj_path):
    """Families in the order their first `usemtl` appears.

    The OBJ importer turns each material group into one static_materials slot in
    this order, so this list is the slot->family contract. Verified against
    bld_001_structure: 13 groups -> 13 slots, same order.
    """
    order = []
    with open(obj_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("usemtl "):
                fam = line.split(None, 1)[1].strip()
                if fam and fam not in order:
                    order.append(fam)
    return order


def import_obj(src, dest, name):
    if not os.path.isfile(src):
        return None
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", src)
    task.set_editor_property("destination_path", dest)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return unreal.load_asset("%s/%s" % (dest, name))


def lookup_material(family):
    """-> the material asset to use for a family, or None.

    A single function so the lookup rule lives in one place. It tries the base
    material first, then a material-instance variant:

        /Game/MC/Families/M_MC_<family>      the tiling master
        /Game/MC/Families/MI_MC_<family>     an optional per-family variant

    The MI fallback exists so a later colour-variant pass can drop
    `MI_MC_<family>` assets in without this script changing. Whichever is found
    is what the slot gets; a miss on both is reported, never guessed.
    """
    for fmt in (MAT_FMT, MI_FMT):
        path = fmt % family
        mat = unreal.load_asset(path)
        if mat is not None:
            return mat, path
    return None, None


def bind_slots(mesh, order, tag, problems):
    """Write the ASSET's static_materials array from the usemtl order.

    Length must match exactly: a mismatch means the importer made a different
    number of groups than the text says, and guessing which family sits where
    would silently mis-texture the mesh. Refuse instead.
    """
    try:
        arr = mesh.get_editor_property("static_materials")
    except Exception as exc:
        problems.append("%s: cannot read static_materials (%s)"
                        % (tag, str(exc)[:60]))
        return False
    n = len(arr) if arr else 0
    if n != len(order):
        problems.append("%s: %d slots but %d usemtl groups %s"
                        % (tag, n, len(order), order))
        return False
    new = []
    for i in range(n):
        fam = order[i]
        mat, path = lookup_material(fam)
        if mat is None:
            problems.append("%s: slot %d wants M_MC_%s (or MI_MC_%s) but "
                            "neither exists" % (tag, i, fam, fam))
            return False
        s = unreal.StaticMaterial()
        try:
            s.set_editor_property("material_slot_name", fam)
        except Exception as exc:
            problems.append("%s: slot name failed (%s)" % (tag, str(exc)[:50]))
        s.set_editor_property("material_interface", mat)
        new.append(s)
    mesh.set_editor_property("static_materials", new)
    return True


def clear_component_overrides(problems):
    """Blank the component-level `override_materials` on every B_/T_ actor.

    This is the fix for the "assets all correct, picture unchanged" bug
    documented in REFACTOR_PLAN.md section 8.12. Engine source, not a guess:
    `FStaticMeshComponentHelper::GetMaterial()`
    (Engine/Source/Runtime/Engine/Public/StaticMeshComponentHelper.h:133-164,
    reached from UStaticMeshComponent::GetMaterial) is

        if (OverrideMaterials.IsValidIndex(i) && OverrideMaterials[i])
            OutMaterial = OverrideMaterials[i];      // override wins
        else if (GetStaticMesh())
            OutMaterial = GetStaticMesh()->GetMaterial(i);   // asset fallback

    so a non-null component override shadows the asset slot completely. The
    level had every structure slot and the terrain's dominant slot pinned to the
    old atlas, which is why the 22 family materials were assigned but never
    sampled. Clearing the overrides makes the asset slots authoritative again --
    the same place the previous "component set_material is ineffective" note
    came from, now correctly attributed.

    Done here, in the pipeline, so a future mesh re-import cannot silently
    reintroduce the overrides.

    Returns (examined, cleared, had_overrides).
    """
    unreal.EditorLoadingAndSavingUtils.load_map(MAP_PATH)
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    actors = unreal.GameplayStatics.get_all_actors_of_class(world,
                                                            unreal.StaticMeshActor)
    examined = cleared = had = 0
    for a in actors:
        lbl = a.get_actor_label()
        if not (lbl.startswith("B_") or lbl.startswith("T_")):
            continue
        sc = a.static_mesh_component
        examined += 1
        try:
            arr = sc.get_editor_property("override_materials") or []
        except Exception as exc:
            problems.append("%s: cannot read override_materials (%s)"
                            % (lbl, str(exc)[:50]))
            continue
        live = [m for m in arr if m is not None]
        if not live:
            continue
        had += 1
        sc.set_editor_property("override_materials", [])
        # Read back: a set_editor_property that silently does nothing is this
        # project's most repeated failure mode.
        after = sc.get_editor_property("override_materials") or []
        if [m for m in after if m is not None]:
            problems.append("%s: %d override(s) survived the clear"
                            % (lbl, len([m for m in after if m is not None])))
            continue
        cleared += 1
    say("component override_materials: examined %d B_/T_ actors, %d had "
        "overrides, %d cleared" % (examined, had, cleared))
    if had and cleared != had:
        problems.append("only %d of %d overridden actors cleared" % (cleared, had))
    return examined, cleared, had


def run_meshes():
    say("=== stage: meshes ===")
    problems = []
    rebound = 0

    struct_manifest = os.path.join(STRUCT_DIR, "manifest.json")
    with open(struct_manifest, encoding="utf-8") as fh:
        man = json.load(fh)

    jobs = []
    for b in man["buildings"]:
        for tag, part in sorted(b["meshes"].items()):
            jobs.append((part["obj"], "bld_%03d_%s" % (b["id"], tag)))
    water = (man.get("water") or man.get("water_layer") or {}).get("obj")
    if water:
        jobs.append((water, os.path.splitext(water)[0]))

    say("structures: %d meshes to reimport" % len(jobs))
    for obj, name in jobs:
        src = os.path.join(STRUCT_DIR, obj)
        mesh = import_obj(src, STRUCT_CONTENT,
                          os.path.splitext(os.path.basename(obj))[0])
        if mesh is None:
            problems.append("%s: import returned no asset" % name)
            continue
        order = usemtl_first_seen_order(src)
        if bind_slots(mesh, order, name, problems):
            rebound += 1
        unreal.EditorAssetLibrary.save_loaded_asset(mesh)

    terrain_manifest = os.path.join(TERRAIN_DIR, "manifest.json")
    with open(terrain_manifest, encoding="utf-8") as fh:
        tman = json.load(fh)
    say("terrain: %d tiles to reimport" % len(tman["tiles"]))
    for t in tman["tiles"]:
        base = os.path.basename(t["obj"])
        src = os.path.join(TERRAIN_DIR, base)
        mesh = import_obj(src, TERRAIN_CONTENT, os.path.splitext(base)[0])
        if mesh is None:
            problems.append("%s: import returned no asset" % base)
            continue
        order = usemtl_first_seen_order(src)
        if bind_slots(mesh, order, base, problems):
            rebound += 1
        unreal.EditorAssetLibrary.save_loaded_asset(mesh)

    say("rebound %d meshes, %d problems" % (rebound, len(problems)))
    for p in problems[:40]:
        say("  PROBLEM %s" % p)

    # --- slot-by-slot read-back from disk ---------------------------------
    # The claim has to come from a fresh load of the saved asset, position by
    # position, not from the write path that just claimed success.
    say("--- verify slot names vs usemtl first-seen order ---")
    probes = [("bld_001_structure", STRUCT_DIR, "bld_001_structure.obj"),
              ("bld_028_structure", STRUCT_DIR, "bld_028_structure.obj"),
              ("bld_001_detail", STRUCT_DIR, "bld_001_detail.obj")]
    terrain_probe_names = sorted(os.path.splitext(os.path.basename(t["obj"]))[0]
                                 for t in tman["tiles"])[:3]
    for nm in terrain_probe_names:
        probes.append((nm, TERRAIN_DIR, nm + ".obj"))

    all_slots_ok = True
    for name, src_dir, obj_file in probes:
        obj_path = os.path.join(src_dir, obj_file)
        if not os.path.isfile(obj_path):
            say("  %-24s OBJ missing, skipped" % name)
            continue
        want = usemtl_first_seen_order(obj_path)
        mesh = unreal.load_asset("/Game/MC/%s"
                                 % ("Structures" if src_dir == STRUCT_DIR
                                    else "Terrain") + "/" + name)
        if mesh is None:
            say("  %-24s *** MISSING ***" % name)
            all_slots_ok = False
            continue
        arr = mesh.get_editor_property("static_materials") or []
        got = []
        for b in arr:
            try:
                got.append(b.get_editor_property("material_slot_name"))
            except Exception:
                got.append("<?>")
        ok = got == want
        all_slots_ok = all_slots_ok and ok
        say("  %-24s slots=%-2d  %s" % (name, len(got),
                                        "OK" if ok else "MISMATCH"))
        say("      want: %s" % want)
        say("      got : %s" % got)
    if not all_slots_ok:
        problems.append("a probed mesh's slot names did not match its usemtl order")

    # --- clear the component overrides that were shadowing the asset slots --
    # NOTE: do NOT load_map() again between this and save_current_level(). The
    # clear is an in-memory edit; a reload would pull the old overrides back off
    # disk and the save would then write them out unchanged. (clear comes after
    # its own load_map; everything after it must stay on the same world.)
    clear_component_overrides(problems)

    # --- save the level the way that actually works -----------------------
    # EditorLoadingAndSavingUtils.save_dirty_packages is a no-op for .umap in
    # this project (proven with a probe actor); save_current_level writes it.
    before_world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    before = unreal.GameplayStatics.get_all_actors_of_class(before_world,
                                                             unreal.Actor)
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    saved = les.save_current_level()
    say("save_current_level -> %s" % saved)

    # Prove persistence by reloading and counting actors.
    unreal.EditorLoadingAndSavingUtils.load_map(MAP_PATH)
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    labels = [a.get_actor_label() for a in
              unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)]
    n_b = sum(1 for x in labels if x.startswith("B_"))
    n_t = sum(1 for x in labels if x.startswith("T_"))
    say("actors before save=%d, after reload=%d  (B_*=%d T_*=%d)"
        % (len(before), len(labels), n_b, n_t))

    # The overrides lived in the level, so a fresh reload is the only honest
    # check that the clear persisted.
    overrides_after = 0
    for a in unreal.GameplayStatics.get_all_actors_of_class(world,
                                                           unreal.StaticMeshActor):
        lbl = a.get_actor_label()
        if not (lbl.startswith("B_") or lbl.startswith("T_")):
            continue
        arr = a.static_mesh_component.get_editor_property("override_materials") or []
        if [m for m in arr if m is not None]:
            overrides_after += 1
    say("after reload: actors still carrying override_materials = %d"
        % overrides_after)
    if overrides_after:
        problems.append("%d actors still carry overrides after reload"
                        % overrides_after)

    shot = "Q:/MC2UE5/shots/family_materials_%s.png" % time.strftime("%Y%m%d_%H%M%S")
    say("--- capture (run from outside the editor) ---")
    say("  python Q:/MC2UE5/tools/focus_capture.py --match mcreplica "
        "--out %s" % shot)

    return not problems and bool(saved)


def run_verify():
    """Read-only audit of everything the two write stages produced.

    A second process loading the assets fresh from disk is the only honest check
    that the writes persisted -- an in-process read-back shares the write cache
    and has already fooled this project once. Nothing here writes: the map is
    only counted, and the material recompiles are not saved.
    """
    say("=== stage: verify (read-only) ===")
    ok = True
    mel = unreal.MaterialEditingLibrary

    # 1. family textures -- Wrap must survive the save
    say("--- 22 textures (address mode) ---")
    bad_t = []
    for fam in FAMILIES:
        tex = unreal.load_asset(TEX_FMT % fam)
        if tex is None:
            bad_t.append(fam)
            say("  %-11s *** MISSING ***" % fam)
            continue
        ax = tex.get_editor_property("address_x")
        ay = tex.get_editor_property("address_y")
        good = (ax == unreal.TextureAddress.TA_WRAP
                and ay == unreal.TextureAddress.TA_WRAP)
        if not good:
            bad_t.append(fam)
        say("  %-11s addr_x=%-8s addr_y=%-8s srgb=%s  %s"
            % (fam, ax.name, ay.name, tex.get_editor_property("srgb"),
               "OK" if good else "*** NOT TA_WRAP ***"))
    ok = ok and not bad_t

    # 2. family materials -- compile + graph, read back from disk
    say("--- 22 materials (recompile + BaseColor) ---")
    bad_m = []
    for fam in FAMILIES:
        mat = unreal.load_asset(MAT_FMT % fam)
        if mat is None:
            bad_m.append(fam)
            say("  %-11s *** MISSING ***" % fam)
            continue
        counts = {}
        for e in mel.get_material_expressions(mat):
            c = e.get_class().get_name()
            counts[c] = counts.get(c, 0) + 1
        offset = current_log_offset()
        mel.recompile_material(mat)          # not saved; read-only check
        comp_ok, why = scan_compile_errors("M_MC_%s" % fam, offset)
        base = mel.get_material_property_input_node(
            mat, unreal.MaterialProperty.MP_BASE_COLOR)
        base_cls = base.get_class().get_name() if base else "NOTHING"
        good = comp_ok and base_cls == "MaterialExpressionTextureSampleParameter2D"
        if not good:
            bad_m.append(fam)
        say("  %-11s compile=%-6s BaseColor<-%s  graph=%s  %s"
            % (fam, "ok" if comp_ok else why[:40], base_cls, counts,
               "OK" if good else "*** FAIL ***"))
    ok = ok and not bad_m

    # 3. mesh slots, re-read from disk, position by position
    say("--- slot names on disk vs usemtl order ---")
    probes = [("bld_001_structure", "Structures", STRUCT_DIR),
              ("bld_028_structure", "Structures", STRUCT_DIR),
              ("bld_001_detail", "Structures", STRUCT_DIR),
              ("terrain_-144_-544", "Terrain", TERRAIN_DIR),
              ("terrain_-144_-416", "Terrain", TERRAIN_DIR),
              ("terrain_-016_-032", "Terrain", TERRAIN_DIR)]
    for name, folder, src_dir in probes:
        obj_path = os.path.join(src_dir, name + ".obj")
        want = usemtl_first_seen_order(obj_path) if os.path.isfile(obj_path) else None
        mesh = unreal.load_asset("/Game/MC/%s/%s" % (folder, name))
        if mesh is None:
            say("  %-24s *** MISSING ***" % name)
            ok = False
            continue
        arr = mesh.get_editor_property("static_materials") or []
        got = [b.get_editor_property("material_slot_name") for b in arr]
        got_s = [str(x) for x in got]
        if want is None:
            say("  %-24s slots=%d  (no obj to compare)" % (name, len(got)))
            continue
        match = got_s == want
        ok = ok and match
        say("  %-24s slots=%-2d %s" % (name, len(got),
                                       "OK" if match else "*** MISMATCH ***"))
        if not match:
            say("      want: %s" % want)
            say("      got : %s" % got_s)

    # 4. the saved level
    unreal.EditorLoadingAndSavingUtils.load_map(MAP_PATH)
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    labels = [a.get_actor_label() for a in
              unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)]
    n_b = sum(1 for x in labels if x.startswith("B_"))
    n_t = sum(1 for x in labels if x.startswith("T_"))
    say("--- level reload: total=%d  B_*=%d  T_*=%d" % (len(labels), n_b, n_t))
    if n_b == 0 or n_t == 0:
        ok = False
        say("*** the level has no B_/T_ actors: the save did not persist")

    if bad_t:
        say("textures not TA_WRAP: %s" % bad_t)
    if bad_m:
        say("materials failing verify: %s" % bad_m)
    return ok


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #

def parse_stage(argv):
    """Resolve the stage: MC_STAGE env > custom argv > default 'all'.

    IMPORTANT -- measured, not assumed: on this setup the editor runs the script
    with **argv == []**, so `--stage=materials` after `-ExecutePythonScript=...`
    never reaches this file (UE's own command-line parser consumes it) and the
    stage silently defaults to 'all'. That is how a materials-only run once fell
    through into the meshes stage. Accepted forms, in priority order:

        MC_STAGE=materials                        (environment -- authoritative)
        -MCstage=materials                        (UE-style custom arg)
        --stage=materials / --stage materials     (plain arg, if ever forwarded)

    Always check the first log line `=== import_family_materials (stage=...) ===`
    to confirm which stage actually ran.
    """
    stage = os.environ.get("MC_STAGE", "").strip().lower()
    source = "MC_STAGE env" if stage else "default"
    for i, a in enumerate(argv):
        low = a.lower()
        if "=" in a and low.startswith(("-mcstage=", "--stage=")):
            stage, source = a.split("=", 1)[1].strip().lower(), "argv"
        elif low in ("-mcstage", "--stage") and i + 1 < len(argv):
            stage, source = argv[i + 1].strip().lower(), "argv"
    if stage not in ("materials", "meshes", "all", "verify"):
        if stage:
            say("ignoring unknown stage %r; using 'all'" % stage)
        stage, source = "all", "default"
    say("stage resolved from %s -> %s" % (source, stage))
    return stage


def main():
    stage = parse_stage(sys.argv)
    say("=== import_family_materials (stage=%s) ===" % stage)
    say("argv=%s" % (sys.argv[1:],))
    try:
        ok = True
        if stage in ("materials", "all"):
            ok = run_materials() and ok
        if stage in ("meshes", "all"):
            if stage == "all" and not ok:
                say("skipping meshes: materials stage did not pass")
            else:
                ok = run_meshes() and ok
        if stage == "verify":
            ok = run_verify() and ok
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())
    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
