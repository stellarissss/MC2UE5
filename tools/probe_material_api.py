# -*- coding: utf-8 -*-
"""
probe_material_api.py -- find a compile check that actually works in UE 5.8.

`build_terrain_material_min.py` calls `mat.get_shader_map_valid()`. On this
engine that attribute does not exist, and its `compiled()` helper wraps the
call in `try/except` returning `(None, "no shader-map query")` -- so every run
reported `compiled=None` and the step-gated early abort it was written to
provide never fired. The check was decorative.

That matters because an uncompilable material is silent: it renders as the
engine default and the cook still reports success. So the check has to be real,
and the only honest way to find the right API is to ask the engine what it has.

Run: UnrealEditor-Cmd.exe MCReplica.uproject \
        -ExecutePythonScript=tools/probe_material_api.py -unattended -nopause
"""

import unreal

OUT = "Q:/MC2UE5/logs/probe_material_api.txt"
_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[PROBE] " + str(msg))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    mat = unreal.load_asset("/Game/MC/Materials/MC_Terrain")
    if mat is None:
        say("MC_Terrain not found; probing the class instead")
        cls = unreal.Material
    else:
        say("probing a live instance: %s" % mat.get_path_name())
        cls = mat.get_class()

    names = [n for n in dir(mat) if not n.startswith("_")] if mat else \
        [n for n in dir(cls) if not n.startswith("_")]
    interesting = [n for n in names
                   if any(k in n.lower() for k in
                          ("shader", "compil", "resource", "error", "cache",
                           "valid", "dirty", "recompile"))]
    say("candidate members (%d):" % len(interesting))
    for n in sorted(interesting):
        say("  %s" % n)

    # Try the plausible ones and record what actually answers.
    for n in sorted(interesting):
        try:
            v = getattr(mat, n)
            if callable(v):
                try:
                    r = v()
                    say("  CALL %s() -> %r" % (n, r))
                except Exception as exc:
                    say("  CALL %s() raised %s" % (n, str(exc)[:100]))
            else:
                say("  GET  %s -> %r" % (n, str(v)[:120]))
        except Exception as exc:
            say("  GET  %s raised %s" % (n, str(exc)[:100]))

    # The documented-ish route: the material's cached resource.
    for n in ("get_material_resource", "get_resource",
              "get_cached_expression_data"):
        if hasattr(mat, n):
            say("has %s" % n)
    say("--- probe done ---")


if __name__ == "__main__":
    main()