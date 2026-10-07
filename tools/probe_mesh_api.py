# -*- coding: utf-8 -*-
"""
probe_mesh_api.py -- find the working vertex-count call in UE 5.8.

`import_chunks.py` guards against the failure in `reimport_terrain.py`, where
the importer silently discarded ~90% of a mesh's geometry. The guard calls
`StaticMeshEditorSubsystem.get_number_verts(mesh, 0)` and that raises
`requires a 'StaticMeshEditorSu...` on this engine -- for every mesh, so the
check never ran and all 158 imports reported "clean" while verifying nothing.

A guard that cannot fail is worse than no guard: it reads as verification in
the log. So find the call that works, and if none does, say so in the output
rather than reporting a pass.

Run: UnrealEditor-Cmd.exe MCReplica.uproject \
        -ExecutePythonScript=tools/probe_mesh_api.py -unattended -nopause
"""

import unreal

OUT = "Q:/MC2UE5/logs/probe_mesh_api.txt"
_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[PROBE4] " + str(msg))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    mesh = unreal.load_asset("/Game/MC/Structures/bld_002_structure")
    if mesh is None:
        say("FATAL: bld_002_structure not found; import something first")
        return
    say("mesh: %s" % mesh.get_path_name())

    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    names = sorted(n for n in dir(sub) if not n.startswith("_"))
    say("subsystem members (%d)" % len(names))
    for n in names:
        if any(k in n.lower() for k in ("vert", "tri", "count", "lod", "num")):
            say("  %s" % n)

    # Try the plausible signatures and record which answer.
    for label, fn in (
            ("get_number_verts(mesh)", lambda: sub.get_number_verts(mesh)),
            ("get_number_verts(mesh,0)", lambda: sub.get_number_verts(mesh, 0)),
            ("get_number_verts(mesh,0,False)",
             lambda: sub.get_number_verts(mesh, 0, False)),
            ("mesh.get_num_triangles(0)", lambda: mesh.get_num_triangles(0)),
            ("mesh.get_num_triangles", lambda: mesh.get_num_triangles()),
            ("mesh.get_bounds", lambda: mesh.get_bounds()),
    ):
        try:
            say("OK   %s -> %r" % (label, fn()))
        except Exception as exc:
            say("FAIL %s -> %s" % (label, str(exc)[:150]))

    # Editor properties that carry the counts, as a fallback.
    for prop in ("num_triangles", "num_vertices", "imported_num_bounds"):
        try:
            say("PROP %s = %r" % (prop, mesh.get_editor_property(prop)))
        except Exception as exc:
            say("PROP %s raised %s" % (prop, str(exc)[:110]))
    say("--- probe done ---")


if __name__ == "__main__":
    main()