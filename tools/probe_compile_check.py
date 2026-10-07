# -*- coding: utf-8 -*-
"""
probe_compile_check.py -- find a way to tell that a material actually compiled.

The first probe found that UE 5.8's Python `Material` has **no** compile-check
member at all: nothing matching shader / compile / error / valid. And
`build_terrain_material_min.py`'s `compiled()` helper hides that behind a
`try/except` that returns `(None, ...)`, so its step-gated abort never fired.

An uncompilable material is the worst kind of failure in this project, because
it is silent: the engine substitutes its default surface, the viewport shows a
checkerboard, and **the cook still reports success**. So a check is worth
finding. Four candidates, cheapest first:

  1. `material_compilation_errors` -- an editor property, may or may not exist
  2. `MaterialEditingLibrary.get_num_material_expressions` -- only proves the
     graph exists, not that it compiles; kept as a control
  3. the `MaterialStats` / shader-map pipeline
  4. the editor log, which is where the engine *does* print
     "Failed to compile Material for platform PCD3D_SM5"

Run: UnrealEditor-Cmd.exe MCReplica.uproject \
        -ExecutePythonScript=tools/probe_compile_check.py -unattended -nopause
"""

import unreal

OUT = "Q:/MC2UE5/logs/probe_compile_check.txt"
MAT = "/Game/MC/Materials/MC_Terrain"
_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[PROBE2] " + str(msg))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    mat = unreal.load_asset(MAT)
    if mat is None:
        say("FATAL: %s not found" % MAT)
        return
    say("material: %s" % mat.get_path_name())

    mel = unreal.MaterialEditingLibrary

    # 1. Editor property, via get_editor_property (raises if absent).
    for prop in ("material_compilation_errors", "compile_errors",
                 "cached_expression_data", "state_id", "bUsedWithNiagara"):
        try:
            v = mat.get_editor_property(prop)
            say("get_editor_property(%s) -> %r" % (prop, str(v)[:200]))
        except Exception as exc:
            say("get_editor_property(%s) raised %s"
                % (prop, str(exc)[:110]))

    # 2. Graph shape (control: proves nothing about compilation).
    try:
        say("num expressions: %d" % len(mel.get_material_expressions(mat)))
    except Exception as exc:
        say("get_material_expressions raised %s" % str(exc)[:110])

    # 3. Look for a stats/validation entry point anywhere on the module.
    hits = [n for n in dir(unreal)
            if any(k in n.lower() for k in
                   ("materialstat", "validation", "shadercompile",
                    "compilerequest", "resourcerequest"))]
    say("module-level candidates (%d): %s" % (len(hits), sorted(hits)[:40]))

    # 4. Does an explicit recompile return anything useful?
    try:
        r = mel.recompile_material(mat)
        say("recompile_material returned %r" % (r,))
    except Exception as exc:
        say("recompile_material raised %s" % str(exc)[:110])

    # 5. Saving is the strongest signal available: the editor refuses to save a
    #    material whose shader map is missing only if we ask it to validate.
    for fn in ("save_asset", "save_loaded_asset"):
        if hasattr(unreal.EditorAssetLibrary, fn):
            try:
                r = getattr(unreal.EditorAssetLibrary, fn)(MAT)
                say("EditorAssetLibrary.%s(%s) -> %s" % (fn, MAT, r))
            except Exception as exc:
                say("EditorAssetLibrary.%s raised %s" % (fn, str(exc)[:110]))

    say("--- probe done ---")


if __name__ == "__main__":
    main()