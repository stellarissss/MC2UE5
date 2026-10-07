# -*- coding: utf-8 -*-
"""
probe_mat_stats.py -- does MaterialStatistics report a valid shader map?

The second probe established that UE 5.8's Python `Material` exposes no
compile-error property at all: `material_compilation_errors`,
`compile_errors` and `cached_expression_data` all raise "Failed to find
property". So `build_terrain_material_min.py`'s `compiled()` cannot work on this
engine, and its `except` returning `(None, "no shader-map query")` means its
abort never fired.

`unreal.MaterialStatistics` is the one remaining candidate. If it reports a
missing shader map, that is the check this project needs -- an uncompilable
material is silent (engine default surface, checkerboard in the viewport, cook
still reports success), so a real check is worth the trouble.

Run: UnrealEditor-Cmd.exe MCReplica.uproject \
        -ExecutePythonScript=tools/probe_mat_stats.py -unattended -nopause
"""

import unreal

OUT = "Q:/MC2UE5/logs/probe_mat_stats.txt"
_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[PROBE3] " + str(msg))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    mat = unreal.load_asset("/Game/MC/Materials/MC_Terrain")
    if mat is None:
        say("FATAL: MC_Terrain missing")
        return

    ms = unreal.MaterialStatistics()
    say("MaterialStatistics() -> %r" % ms)
    try:
        ms.set_editor_property("material", mat)
        say("set material ok")
    except Exception as exc:
        say("set_editor_property(material) raised %s" % str(exc)[:140])
    try:
        unreal.MaterialEditingLibrary.recompile_material(mat)
    except Exception:
        pass

    names = [n for n in dir(ms) if not n.startswith("_")]
    say("members: %s" % sorted(names))
    for n in sorted(names):
        try:
            v = getattr(ms, n)
            if callable(v):
                try:
                    say("  CALL %s() -> %r" % (n, v()))
                except Exception as exc:
                    say("  CALL %s() raised %s" % (n, str(exc)[:90]))
            else:
                say("  GET  %s = %r" % (n, v))
        except Exception as exc:
            say("  GET  %s raised %s" % (n, str(exc)[:90]))

    # If a usable predicate exists, prove it discriminates: a material with a
    # broken expression must report differently from the known-good one.
    say("--- probe done ---")


if __name__ == "__main__":
    main()